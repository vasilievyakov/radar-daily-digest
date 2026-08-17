import json
import os
import subprocess
from datetime import date

import httpx
import pytest
from pydantic import BaseModel

from radar.cache import ModelCache
from radar.db import init_db
from radar.llm import API_KEY_ENV, OpenRouterClient, SchemaValidationError
from radar.llm_cli import (
    BACKEND_CLI,
    BACKEND_OPENROUTER,
    CLI_BIN_ENV,
    INTEGRATION_ENV,
    ClaudeCLIClient,
    ClaudeCLIError,
    ClaudeCLINotFound,
    ClaudeCLITimeout,
    UnknownModel,
    cli_model,
    make_backend,
)
from radar.runlog import Budget, BudgetExceeded, RunLog, new_run_id

FAKE_BIN = "/opt/fake/claude"
HAIKU = "anthropic/claude-haiku-4.5"
SONNET = "anthropic/claude-sonnet-5"


def cli_payload(
    result: str = "hello",
    *,
    cost: float | None = 0.005,
    input_tokens: int = 12,
    output_tokens: int = 30,
    cache_read: int = 14499,
    cache_creation: int = 0,
    is_error: bool = False,
) -> dict:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "duration_ms": 6013,
        "duration_api_ms": 5904,
        "num_turns": 1,
        "result": result,
        "session_id": "3f2a",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
        "modelUsage": {
            "claude-haiku-4-5-20251001": {
                "costUSD": cost or 0.0,
                "canonicalModel": "claude-haiku-4-5-20251001",
                "provider": "firstParty",
            }
        },
        "stop_reason": "end_turn",
    }
    if cost is not None:
        payload["total_cost_usd"] = cost
    return payload


class FakeCLI:
    """Stands in for subprocess.run and records every invocation."""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, subprocess.CompletedProcess):
            return item
        return subprocess.CompletedProcess(argv, 0, json.dumps(item), "")

    @property
    def argv(self) -> list[str]:
        return self.calls[0]["argv"]

    def stdin(self, index: int = 0) -> str:
        return self.calls[index]["input"]


@pytest.fixture(autouse=True)
def sealed_env(monkeypatch):
    """No real key, no real binary, no real process."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.setenv(CLI_BIN_ENV, FAKE_BIN)


@pytest.fixture
def run_log(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    log = RunLog(conn, new_run_id(), date(2026, 8, 17))
    yield log
    conn.close()


def make_client(fake, monkeypatch, tmp_path, **kwargs):
    monkeypatch.setattr(subprocess, "run", fake)
    return ClaudeCLIClient(cache=ModelCache(tmp_path / "cache"), **kwargs)


def model_call_rows(log: RunLog) -> list[dict]:
    cursor = log.conn.execute(
        "SELECT stage, model, provider, tokens_in, tokens_out, cost_usd, cached "
        "FROM model_calls WHERE run_id = ? ORDER BY rowid",
        (log.run_id,),
    )
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


# -- command line ------------------------------------------------------


def test_command_line_carries_every_trim_flag(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload()])
    client = make_client(fake, monkeypatch, tmp_path)

    client.complete("hi", model=HAIKU, stage="filter")

    argv = fake.argv
    assert argv[0] == FAKE_BIN
    assert "-p" in argv
    assert flag_value(argv, "--model") == "haiku"
    assert flag_value(argv, "--output-format") == "json"
    assert flag_value(argv, "--max-turns") == "1"
    assert "--dangerously-skip-permissions" in argv
    # No tools, no MCP servers, no settings files: this is where the ~$0.027
    # of overhead per call went.
    assert flag_value(argv, "--allowed-tools") == ""
    assert "--strict-mcp-config" in argv
    assert flag_value(argv, "--mcp-config") == '{"mcpServers":{}}'
    assert flag_value(argv, "--setting-sources") == ""
    assert "--exclude-dynamic-system-prompt-sections" in argv
    assert flag_value(argv, "--system-prompt")

    call = fake.calls[0]
    assert call["shell"] is False
    assert call["text"] is True


def test_prompt_goes_on_stdin_not_argv(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload()])
    client = make_client(fake, monkeypatch, tmp_path)

    client.complete("the material text", model=HAIKU, stage="filter")

    assert fake.stdin() == "the material text"
    assert "the material text" not in fake.argv


def test_system_prompt_is_prefix_then_system(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload()])
    client = make_client(fake, monkeypatch, tmp_path)

    client.complete(
        "material",
        model=HAIKU,
        stage="filter",
        system="Answer yes or no.",
        cache_prefix="Relevance criteria: ...",
    )

    assert flag_value(fake.argv, "--system-prompt") == (
        "Relevance criteria: ...\n\nAnswer yes or no."
    )


def test_changing_system_prompt_within_a_stage_is_noted(monkeypatch, tmp_path, run_log):
    fake = FakeCLI([cli_payload()])
    client = make_client(fake, monkeypatch, tmp_path)
    args = {"model": HAIKU, "stage": "filter", "run_log": run_log}

    client.complete("a", system="Prompt A", **args)
    client.complete("b", system="Prompt A", **args)
    assert run_log.notes == []

    client.complete("c", system="Prompt B", **args)
    assert any("prompt cache will miss" in note for note in run_log.notes)


# -- response parsing --------------------------------------------------


def test_successful_response_is_parsed(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload("the answer", cost=0.0051, cache_read=14499)])
    client = make_client(fake, monkeypatch, tmp_path)

    result = client.complete("hi", model=HAIKU, stage="filter")

    assert result.text == "the answer"
    assert result.cost_usd == pytest.approx(0.0051)
    assert result.original_cost_usd == pytest.approx(0.0051)
    # Cache tokens are the bill, so they are counted in.
    assert result.tokens_in == 12 + 14499
    assert result.tokens_out == 30
    assert result.cached_tokens == 14499
    assert result.provider == "claude-cli"
    assert result.model == HAIKU
    assert result.cached is False
    assert result.finish_reason == "end_turn"


def test_cache_write_tokens_are_counted_too(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload(cache_read=0, cache_creation=14499)])
    client = make_client(fake, monkeypatch, tmp_path)

    result = client.complete("hi", model=HAIKU, stage="filter")

    assert result.tokens_in == 12 + 14499
    assert result.cached_tokens == 0


SCHEMA = {
    "title": "verdict",
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "reason_code": {"type": "string"},
    },
    "required": ["relevant", "reason_code"],
    "additionalProperties": False,
}


def test_schema_goes_into_the_prompt_not_the_system_prompt(monkeypatch, tmp_path):
    answer = json.dumps({"relevant": True, "reason_code": "breaking_change"})
    fake = FakeCLI([cli_payload(answer)])
    client = make_client(fake, monkeypatch, tmp_path)

    result = client.complete("material", model=HAIKU, stage="filter", schema=SCHEMA)

    assert "JSON Schema" in fake.stdin()
    assert "reason_code" in fake.stdin()
    assert "reason_code" not in flag_value(fake.argv, "--system-prompt")
    assert result.data == {"relevant": True, "reason_code": "breaking_change"}


def test_json_is_extracted_from_a_markdown_fence(monkeypatch, tmp_path):
    fenced = "```json\n" + json.dumps({"relevant": True, "reason_code": "x"}) + "\n```"
    fake = FakeCLI([cli_payload(fenced)])
    client = make_client(fake, monkeypatch, tmp_path)

    result = client.complete("material", model=HAIKU, stage="filter", schema=SCHEMA)

    assert result.data["reason_code"] == "x"


def test_json_is_extracted_from_surrounding_prose(monkeypatch, tmp_path):
    text = (
        "Sure, here is the verdict:\n"
        '{"relevant": false, "reason_code": "marketing {noise}"}\n'
        "Let me know if you need more."
    )
    fake = FakeCLI([cli_payload(text)])
    client = make_client(fake, monkeypatch, tmp_path)

    result = client.complete("material", model=HAIKU, stage="filter", schema=SCHEMA)

    # The brace inside the string must not end the object early.
    assert result.data == {"relevant": False, "reason_code": "marketing {noise}"}


class Verdict(BaseModel):
    relevant: bool
    reason_code: str = "none"


def test_pydantic_schema_is_accepted(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload(json.dumps({"relevant": True}))])
    client = make_client(fake, monkeypatch, tmp_path)

    result = client.complete("material", model=HAIKU, stage="filter", schema=Verdict)

    assert result.data == {"relevant": True, "reason_code": "none"}


# -- schema retries ----------------------------------------------------


def test_schema_retry_feeds_the_error_back(monkeypatch, tmp_path):
    good = json.dumps({"relevant": False, "reason_code": "marketing"})
    fake = FakeCLI([cli_payload("no idea, sorry"), cli_payload(good)])
    client = make_client(fake, monkeypatch, tmp_path)

    result = client.complete("material", model=HAIKU, stage="filter", schema=SCHEMA)

    assert result.data == {"relevant": False, "reason_code": "marketing"}
    assert result.attempts == 2
    assert len(fake.calls) == 2
    retry_prompt = fake.stdin(1)
    assert "did not match the required JSON schema" in retry_prompt
    assert "no idea, sorry" in retry_prompt


def test_schema_violation_is_retried(monkeypatch, tmp_path):
    fake = FakeCLI(
        [
            cli_payload(json.dumps({"relevant": True})),
            cli_payload(json.dumps({"relevant": True, "reason_code": "ok"})),
        ]
    )
    client = make_client(fake, monkeypatch, tmp_path)

    result = client.complete("material", model=HAIKU, stage="filter", schema=SCHEMA)

    assert result.data["reason_code"] == "ok"
    assert len(fake.calls) == 2


def test_schema_gives_up_after_limited_retries(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload("not json at all", cost=0.004)])
    client = make_client(fake, monkeypatch, tmp_path, max_schema_retries=1)
    budget = Budget(limit_usd=10.0)

    with pytest.raises(SchemaValidationError):
        client.complete(
            "material",
            model=HAIKU,
            stage="filter",
            schema=SCHEMA,
            budget=budget,
        )

    assert len(fake.calls) == 2
    # Every retry is a real call and every one was charged.
    assert budget.spent_usd == pytest.approx(0.008)


# -- cache -------------------------------------------------------------


def test_cache_miss_then_hit(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload("answer", cost=0.005)])
    client = make_client(fake, monkeypatch, tmp_path)
    args = {"model": HAIKU, "stage": "filter"}

    first = client.complete("same prompt", **args)
    second = client.complete("same prompt", **args)

    assert len(fake.calls) == 1
    assert first.cached is False
    assert second.cached is True
    assert second.text == first.text == "answer"
    assert second.provider == "claude-cli"
    assert second.original_cost_usd == pytest.approx(0.005)


def test_different_prompt_is_a_miss(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload()])
    client = make_client(fake, monkeypatch, tmp_path)
    args = {"model": HAIKU, "stage": "filter"}

    client.complete("prompt one", **args)
    client.complete("prompt two", **args)

    assert len(fake.calls) == 2


def test_cache_hit_spends_nothing(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload(cost=0.25)])
    client = make_client(fake, monkeypatch, tmp_path)
    budget = Budget(limit_usd=1.0)
    args = {"model": HAIKU, "stage": "filter", "budget": budget}

    client.complete("same prompt", **args)
    assert budget.spent_usd == pytest.approx(0.25)

    second = client.complete("same prompt", **args)
    assert budget.spent_usd == pytest.approx(0.25)
    assert second.cost_usd == 0.0
    assert second.cached is True
    assert len(fake.calls) == 1


def test_cache_key_is_shared_with_the_openrouter_backend(monkeypatch, tmp_path):
    """A cache warmed over the API is still warm when the CLI takes over."""
    cache_root = tmp_path / "cache"
    openrouter_payload = {
        "id": "gen-1",
        "model": SONNET,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "archived answer"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "cost": 0.004},
        "provider": "Anthropic",
    }
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=openrouter_payload)
        )
    )
    warm = OpenRouterClient(
        api_key="test-key", cache=ModelCache(cache_root), client=http
    )
    warm.complete("shared prompt", model=SONNET, stage="filter", system="Be brief.")

    fake = FakeCLI([cli_payload("should not be called")])
    monkeypatch.setattr(subprocess, "run", fake)
    cli = ClaudeCLIClient(cache=ModelCache(cache_root))

    replayed = cli.complete(
        "shared prompt", model=SONNET, stage="filter", system="Be brief."
    )

    assert replayed.cached is True
    assert replayed.text == "archived answer"
    assert fake.calls == []


def test_cli_entries_are_readable_by_the_openrouter_backend(monkeypatch, tmp_path):
    cache_root = tmp_path / "cache"
    fake = FakeCLI([cli_payload("written by the cli")])
    monkeypatch.setattr(subprocess, "run", fake)
    cli = ClaudeCLIClient(cache=ModelCache(cache_root))
    cli.complete("shared prompt", model=SONNET, stage="enrich")

    keyless = OpenRouterClient(cache=ModelCache(cache_root))
    replayed = keyless.complete("shared prompt", model=SONNET, stage="enrich")

    assert replayed.cached is True
    assert replayed.text == "written by the cli"
    assert len(fake.calls) == 1


# -- budget ------------------------------------------------------------


def test_budget_exceeded_before_the_process_starts(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload()])
    client = make_client(fake, monkeypatch, tmp_path)
    budget = Budget(limit_usd=0.01)
    budget.charge(0.009)

    with pytest.raises(BudgetExceeded):
        client.complete(
            "hi",
            model=HAIKU,
            stage="enrich",
            budget=budget,
            estimated_usd=0.05,
        )

    # The point of checking first: no process was ever spawned.
    assert fake.calls == []


def test_max_budget_flag_comes_from_the_remaining_budget(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload(cost=0.0329)])
    client = make_client(fake, monkeypatch, tmp_path)
    budget = Budget(limit_usd=0.50)
    budget.charge(0.10)

    client.complete("hi", model=HAIKU, stage="filter", budget=budget)

    assert flag_value(fake.argv, "--max-budget-usd") == "0.4000"
    assert budget.spent_usd == pytest.approx(0.1329)


def test_no_budget_means_no_max_budget_flag(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload()])
    client = make_client(fake, monkeypatch, tmp_path)

    client.complete("hi", model=HAIKU, stage="filter")

    assert "--max-budget-usd" not in fake.argv


def test_missing_cost_is_noted_not_guessed(monkeypatch, tmp_path, run_log):
    fake = FakeCLI([cli_payload(cost=None)])
    client = make_client(fake, monkeypatch, tmp_path)

    result = client.complete("hi", model=HAIKU, stage="enrich", run_log=run_log)

    assert result.cost_usd == 0.0
    assert any("total_cost_usd" in note for note in run_log.notes)


# -- process failures --------------------------------------------------


def test_timeout_becomes_a_clear_error(monkeypatch, tmp_path):
    fake = FakeCLI([subprocess.TimeoutExpired(cmd="claude", timeout=1.0)])
    client = make_client(fake, monkeypatch, tmp_path, timeout=1.0)

    with pytest.raises(ClaudeCLITimeout, match="did not answer within"):
        client.complete("hi", model=HAIKU, stage="filter")


def test_nonzero_exit_carries_stderr(monkeypatch, tmp_path):
    fake = FakeCLI(
        [subprocess.CompletedProcess(["claude"], 2, "", "Error: model not found")]
    )
    client = make_client(fake, monkeypatch, tmp_path)

    with pytest.raises(ClaudeCLIError, match="model not found"):
        client.complete("hi", model=HAIKU, stage="filter")


def test_is_error_payload_raises(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload("budget exceeded", is_error=True)])
    client = make_client(fake, monkeypatch, tmp_path)

    with pytest.raises(ClaudeCLIError, match="budget exceeded"):
        client.complete("hi", model=HAIKU, stage="filter")


def test_non_json_stdout_raises(monkeypatch, tmp_path):
    fake = FakeCLI([subprocess.CompletedProcess(["claude"], 0, "not json", "")])
    client = make_client(fake, monkeypatch, tmp_path)

    with pytest.raises(ClaudeCLIError, match="did not return JSON"):
        client.complete("hi", model=HAIKU, stage="filter")


def test_missing_binary_is_explicit(monkeypatch, tmp_path):
    monkeypatch.delenv(CLI_BIN_ENV, raising=False)
    monkeypatch.setattr("radar.llm_cli.shutil.which", lambda _: None)
    fake = FakeCLI([cli_payload()])
    client = make_client(fake, monkeypatch, tmp_path)

    with pytest.raises(ClaudeCLINotFound, match="not found on PATH"):
        client.complete("hi", model=HAIKU, stage="filter")

    assert fake.calls == []


def test_missing_binary_still_serves_the_cache(monkeypatch, tmp_path):
    """A cached replay must run on a machine with no CLI installed."""
    cache_root = tmp_path / "cache"
    fake = FakeCLI([cli_payload("archived")])
    monkeypatch.setattr(subprocess, "run", fake)
    ClaudeCLIClient(cache=ModelCache(cache_root)).complete(
        "prompt", model=HAIKU, stage="filter"
    )

    monkeypatch.delenv(CLI_BIN_ENV, raising=False)
    monkeypatch.setattr("radar.llm_cli.shutil.which", lambda _: None)
    replayed = ClaudeCLIClient(cache=ModelCache(cache_root)).complete(
        "prompt", model=HAIKU, stage="filter"
    )

    assert replayed.cached is True
    assert replayed.text == "archived"


# -- run log -----------------------------------------------------------


def test_run_log_records_the_call(monkeypatch, tmp_path, run_log):
    fake = FakeCLI([cli_payload(cost=0.0329, cache_read=0, cache_creation=14499)])
    client = make_client(fake, monkeypatch, tmp_path)

    client.complete("hi", model=SONNET, stage="filter", run_log=run_log)

    rows = model_call_rows(run_log)
    assert len(rows) == 1
    assert rows[0]["stage"] == "filter"
    assert rows[0]["model"] == SONNET
    assert rows[0]["provider"] == "claude-cli"
    assert rows[0]["tokens_in"] == 12 + 14499
    assert rows[0]["tokens_out"] == 30
    assert rows[0]["cost_usd"] == pytest.approx(0.0329)
    assert rows[0]["cached"] == 0
    assert run_log.cost_usd == pytest.approx(0.0329)


def test_run_log_marks_cache_hits(monkeypatch, tmp_path, run_log):
    fake = FakeCLI([cli_payload(cost=0.01)])
    client = make_client(fake, monkeypatch, tmp_path)
    args = {"model": HAIKU, "stage": "filter", "run_log": run_log}

    client.complete("same", **args)
    client.complete("same", **args)

    rows = model_call_rows(run_log)
    assert [row["cached"] for row in rows] == [0, 1]
    assert [row["cost_usd"] for row in rows] == [pytest.approx(0.01), 0.0]
    assert run_log.model_calls == 2
    assert run_log.cost_usd == pytest.approx(0.01)


# -- model names -------------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("anthropic/claude-opus-5", "opus"),
        ("anthropic/claude-sonnet-5", "sonnet"),
        ("anthropic/claude-haiku-4.5", "haiku"),
        ("anthropic/claude-fable-5", "fable"),
        # A pinned older version keeps its full identifier: the short alias
        # would silently move to the current model of that family.
        ("anthropic/claude-opus-4.8", "claude-opus-4-8"),
        ("anthropic/claude-sonnet-4.6", "claude-sonnet-4-6"),
        # Already CLI-native.
        ("haiku", "haiku"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    ],
)
def test_model_slugs_map_to_cli_names(slug, expected):
    assert cli_model(slug) == expected


@pytest.mark.parametrize("slug", ["openai/gpt-5", "anthropic/claude-opus-9", "gemini"])
def test_unknown_slug_raises(slug):
    with pytest.raises(UnknownModel, match="no Claude CLI model"):
        cli_model(slug)


def test_unknown_slug_raises_before_the_process_starts(monkeypatch, tmp_path):
    fake = FakeCLI([cli_payload()])
    client = make_client(fake, monkeypatch, tmp_path)

    with pytest.raises(UnknownModel):
        client.complete("hi", model="openai/gpt-5", stage="filter")

    assert fake.calls == []


# -- backend selection -------------------------------------------------


def test_factory_picks_openrouter_when_the_key_is_set(monkeypatch, run_log):
    monkeypatch.setenv(API_KEY_ENV, "sk-or-test")

    backend = make_backend(run_log=run_log)

    assert isinstance(backend, OpenRouterClient)
    assert any(BACKEND_OPENROUTER in note for note in run_log.notes)


def test_factory_picks_the_cli_without_a_key(run_log):
    backend = make_backend(run_log=run_log)

    assert isinstance(backend, ClaudeCLIClient)
    assert any(BACKEND_CLI in note for note in run_log.notes)


def test_prefer_overrides_the_environment(monkeypatch, run_log):
    monkeypatch.setenv(API_KEY_ENV, "sk-or-test")

    backend = make_backend(prefer="claude-cli", run_log=run_log)

    assert isinstance(backend, ClaudeCLIClient)
    assert any("explicit preference" in note for note in run_log.notes)


def test_prefer_openrouter_without_a_key_still_builds_the_client():
    # Key resolution is lazy, so a cached replay can run keyless.
    assert isinstance(make_backend(prefer="openrouter"), OpenRouterClient)


def test_config_can_pin_the_backend():
    config = {"models": {"backend": "claude-cli", "filter": SONNET}}

    assert isinstance(make_backend(config), ClaudeCLIClient)


def test_unknown_backend_name_raises():
    with pytest.raises(ValueError, match="unknown model backend"):
        make_backend(prefer="ollama")


def test_factory_passes_the_cache_through(tmp_path):
    cache = ModelCache(tmp_path / "cache")

    backend = make_backend(prefer="claude-cli", cache=cache)

    assert backend.cache is cache


# -- integration -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get(INTEGRATION_ENV) != "1",
    reason=f"set {INTEGRATION_ENV}=1 to call the real claude CLI",
)
def test_real_cli_answers(tmp_path, monkeypatch):
    monkeypatch.delenv(CLI_BIN_ENV, raising=False)
    client = ClaudeCLIClient(cache=ModelCache(tmp_path / "cache"), timeout=120.0)
    budget = Budget(limit_usd=0.20)

    result = client.complete(
        "Reply with the single word OK and nothing else.",
        model=HAIKU,
        stage="smoke",
        budget=budget,
    )

    assert "OK" in result.text.upper()
    assert result.provider == "claude-cli"
    assert result.cost_usd > 0
    assert budget.spent_usd == pytest.approx(result.cost_usd)
