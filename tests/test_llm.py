import json
from datetime import date

import httpx
import pytest
from pydantic import BaseModel

from radar.cache import ModelCache
from radar.db import init_db
from radar.llm import (
    API_KEY_ENV,
    DEFAULT_STAGE_MODELS,
    LLMError,
    MissingAPIKey,
    OpenRouterClient,
    SchemaValidationError,
    embedding_model,
    model_for_stage,
    stage_models,
)
from radar.runlog import Budget, BudgetExceeded, RunLog, new_run_id


def chat_payload(
    content: str = "hello",
    *,
    cost: float | None = 0.004,
    provider: str = "Anthropic",
    model: str = "anthropic/claude-sonnet-5",
    cached_tokens: int = 0,
) -> dict:
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "prompt_tokens_details": {
            "cached_tokens": cached_tokens,
            "cache_write_tokens": 0,
        },
    }
    if cost is not None:
        usage["cost"] = cost
        usage["cost_details"] = {"upstream_inference_cost": cost}
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
        "openrouter_metadata": {
            "requested": model,
            "strategy": "direct",
            "attempt": 1,
            "endpoints": {
                "total": 1,
                "available": [{"provider": provider, "model": model, "selected": True}],
            },
            "attempts": [{"provider": provider, "model": model, "status": 200}],
        },
    }


class Recorder:
    """Mock transport that records every request body it was handed."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content or b"{}"))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, httpx.Response):
            return item
        status, body = item
        return httpx.Response(status, json=body)


@pytest.fixture(autouse=True)
def no_ambient_key(monkeypatch):
    """Nothing in this file may accidentally read a real key."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)


def make_client(recorder, tmp_path, *, api_key="test-key", **kwargs):
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    return OpenRouterClient(
        api_key=api_key,
        cache=ModelCache(tmp_path / "cache"),
        client=http,
        sleep=lambda _: None,
        **kwargs,
    )


@pytest.fixture
def run_log(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    log = RunLog(conn, new_run_id(), date(2026, 8, 17))
    yield log
    conn.close()


def model_call_rows(log: RunLog) -> list[dict]:
    cursor = log.conn.execute(
        "SELECT stage, model, provider, tokens_in, tokens_out, cost_usd, cached "
        "FROM model_calls WHERE run_id = ? ORDER BY rowid",
        (log.run_id,),
    )
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# -- request shape -----------------------------------------------------


def test_provider_is_pinned_and_temperature_zero(tmp_path):
    recorder = Recorder([(200, chat_payload())])
    client = make_client(recorder, tmp_path)

    client.complete("hi", model="anthropic/claude-sonnet-5", stage="filter")

    body = recorder.bodies[0]
    assert body["temperature"] == 0.0
    assert body["top_p"] == 1.0
    assert body["provider"]["order"] == ["anthropic"]
    assert body["provider"]["only"] == ["anthropic"]
    assert body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["require_parameters"] is True
    assert body["seed"] == 7

    request = recorder.requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    # Without the opt-in header the response carries no provider identity.
    assert request.headers["x-openrouter-metadata"] == "enabled"


def test_provider_override_wins_over_slug_prefix(tmp_path):
    recorder = Recorder([(200, chat_payload())])
    client = make_client(
        recorder, tmp_path, provider_overrides={"meta-llama/llama-4": "deepinfra"}
    )

    client.complete("hi", model="meta-llama/llama-4", stage="cluster")

    assert recorder.bodies[0]["provider"]["order"] == ["deepinfra"]


def test_recorded_provider_comes_from_openrouter_metadata(tmp_path):
    recorder = Recorder([(200, chat_payload(provider="Amazon Bedrock"))])
    client = make_client(recorder, tmp_path)

    result = client.complete("hi", model="anthropic/claude-sonnet-5", stage="filter")

    assert result.provider == "Amazon Bedrock"


def test_legacy_top_level_provider_field_is_read(tmp_path):
    payload = chat_payload()
    payload.pop("openrouter_metadata")
    payload["provider"] = "Anthropic"
    recorder = Recorder([(200, payload)])
    client = make_client(recorder, tmp_path)

    result = client.complete("hi", model="anthropic/claude-sonnet-5", stage="filter")

    assert result.provider == "Anthropic"


def test_cache_prefix_is_marked_cacheable(tmp_path):
    recorder = Recorder([(200, chat_payload())])
    client = make_client(recorder, tmp_path)

    client.complete(
        "material text",
        model="anthropic/claude-sonnet-5",
        stage="filter",
        system="Answer with yes or no.",
        cache_prefix="Relevance criteria: ...",
    )

    system = recorder.bodies[0]["messages"][0]
    assert system["role"] == "system"
    assert system["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert system["content"][0]["text"] == "Relevance criteria: ..."
    # The per-call system prompt is deliberately not marked: it varies.
    assert "cache_control" not in system["content"][1]


def test_cache_prefix_changes_the_cache_key(tmp_path):
    recorder = Recorder([(200, chat_payload())])
    client = make_client(recorder, tmp_path)
    args = {"model": "anthropic/claude-sonnet-5", "stage": "filter"}

    client.complete("p", cache_prefix="A", **args)
    client.complete("p", cache_prefix="B", **args)

    assert len(recorder.requests) == 2


def test_cached_tokens_are_reported(tmp_path):
    recorder = Recorder([(200, chat_payload(cached_tokens=900))])
    client = make_client(recorder, tmp_path)

    result = client.complete("hi", model="anthropic/claude-sonnet-5", stage="filter")

    assert result.cached_tokens == 900


# -- cache -------------------------------------------------------------


def test_cache_miss_then_hit(tmp_path):
    recorder = Recorder([(200, chat_payload("answer"))])
    client = make_client(recorder, tmp_path)
    args = {"model": "anthropic/claude-sonnet-5", "stage": "filter"}

    first = client.complete("same prompt", **args)
    second = client.complete("same prompt", **args)

    assert len(recorder.requests) == 1
    assert first.cached is False
    assert second.cached is True
    assert second.text == first.text == "answer"
    assert second.provider == "Anthropic"
    assert second.original_cost_usd == pytest.approx(0.004)


def test_different_prompt_is_a_miss(tmp_path):
    recorder = Recorder([(200, chat_payload())])
    client = make_client(recorder, tmp_path)
    args = {"model": "anthropic/claude-sonnet-5", "stage": "filter"}

    client.complete("prompt one", **args)
    client.complete("prompt two", **args)

    assert len(recorder.requests) == 2


def test_cache_hit_costs_nothing(tmp_path):
    recorder = Recorder([(200, chat_payload(cost=0.25))])
    client = make_client(recorder, tmp_path)
    budget = Budget(limit_usd=1.0)
    args = {"model": "anthropic/claude-sonnet-5", "stage": "filter", "budget": budget}

    client.complete("same prompt", **args)
    assert budget.spent_usd == pytest.approx(0.25)

    second = client.complete("same prompt", **args)
    assert budget.spent_usd == pytest.approx(0.25)
    assert second.cost_usd == 0.0
    assert second.cached is True


def test_cached_replay_needs_no_api_key(tmp_path):
    recorder = Recorder([(200, chat_payload("archived"))])
    cache_root = tmp_path / "cache"
    warm = OpenRouterClient(
        api_key="test-key",
        cache=ModelCache(cache_root),
        client=httpx.Client(transport=httpx.MockTransport(recorder)),
        sleep=lambda _: None,
    )
    warm.complete("prompt", model="anthropic/claude-opus-5", stage="enrich")

    keyless = OpenRouterClient(
        cache=ModelCache(cache_root),
        client=httpx.Client(transport=httpx.MockTransport(recorder)),
        sleep=lambda _: None,
    )
    replayed = keyless.complete(
        "prompt", model="anthropic/claude-opus-5", stage="enrich"
    )

    assert replayed.text == "archived"
    assert replayed.cached is True
    assert len(recorder.requests) == 1


# -- budget ------------------------------------------------------------


def test_budget_exceeded_before_the_request(tmp_path):
    recorder = Recorder([(200, chat_payload())])
    client = make_client(recorder, tmp_path)
    budget = Budget(limit_usd=0.01)
    budget.charge(0.009)

    with pytest.raises(BudgetExceeded):
        client.complete(
            "hi",
            model="anthropic/claude-opus-5",
            stage="enrich",
            budget=budget,
            estimated_usd=0.05,
        )

    # The point of checking before the call: nothing left the machine.
    assert recorder.requests == []


def test_actual_cost_from_response_is_charged(tmp_path):
    recorder = Recorder([(200, chat_payload(cost=0.0731))])
    client = make_client(recorder, tmp_path)
    budget = Budget(limit_usd=1.0)

    result = client.complete(
        "hi",
        model="anthropic/claude-opus-5",
        stage="enrich",
        budget=budget,
        estimated_usd=0.01,
    )

    assert result.cost_usd == pytest.approx(0.0731)
    assert budget.spent_usd == pytest.approx(0.0731)


def test_missing_cost_is_noted_not_guessed(tmp_path, run_log):
    recorder = Recorder([(200, chat_payload(cost=None))])
    client = make_client(recorder, tmp_path)

    result = client.complete(
        "hi", model="anthropic/claude-opus-5", stage="enrich", run_log=run_log
    )

    assert result.cost_usd == 0.0
    assert any("no usage.cost" in note for note in run_log.notes)


# -- run log -----------------------------------------------------------


def test_run_log_records_model_and_provider(tmp_path, run_log):
    recorder = Recorder([(200, chat_payload(cost=0.01))])
    client = make_client(recorder, tmp_path)

    client.complete(
        "hi", model="anthropic/claude-sonnet-5", stage="filter", run_log=run_log
    )

    rows = model_call_rows(run_log)
    assert len(rows) == 1
    assert rows[0]["stage"] == "filter"
    assert rows[0]["model"] == "anthropic/claude-sonnet-5"
    assert rows[0]["provider"] == "Anthropic"
    assert rows[0]["tokens_in"] == 120
    assert rows[0]["tokens_out"] == 30
    assert rows[0]["cost_usd"] == pytest.approx(0.01)
    assert rows[0]["cached"] == 0
    assert run_log.cost_usd == pytest.approx(0.01)


def test_run_log_marks_cache_hits(tmp_path, run_log):
    recorder = Recorder([(200, chat_payload(cost=0.01))])
    client = make_client(recorder, tmp_path)
    args = {
        "model": "anthropic/claude-sonnet-5",
        "stage": "filter",
        "run_log": run_log,
    }

    client.complete("same", **args)
    client.complete("same", **args)

    rows = model_call_rows(run_log)
    assert [row["cached"] for row in rows] == [0, 1]
    assert [row["cost_usd"] for row in rows] == [pytest.approx(0.01), 0.0]
    # Two calls happened; only one was paid for.
    assert run_log.model_calls == 2
    assert run_log.cost_usd == pytest.approx(0.01)


# -- retries -----------------------------------------------------------


def test_retry_on_429(tmp_path):
    recorder = Recorder(
        [
            (429, {"error": {"code": 429, "message": "rate limited"}}),
            (200, chat_payload("after backoff")),
        ]
    )
    delays: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    client = OpenRouterClient(
        api_key="test-key",
        cache=ModelCache(tmp_path / "cache"),
        client=http,
        sleep=delays.append,
    )

    result = client.complete("hi", model="anthropic/claude-sonnet-5", stage="filter")

    assert result.text == "after backoff"
    assert len(recorder.requests) == 2
    assert delays == [1.0]


def test_retry_honours_retry_after_header(tmp_path):
    responses = [
        httpx.Response(429, json={"error": "slow down"}, headers={"retry-after": "3"}),
        httpx.Response(200, json=chat_payload()),
    ]
    recorder = Recorder(responses)
    delays: list[float] = []
    client = OpenRouterClient(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(recorder)),
        sleep=delays.append,
    )

    client.complete("hi", model="anthropic/claude-sonnet-5", stage="filter")

    assert delays == [3.0]


def test_retry_on_5xx_then_gives_up(tmp_path):
    recorder = Recorder([(503, {"error": "upstream down"})])
    delays: list[float] = []
    client = OpenRouterClient(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(recorder)),
        max_retries=2,
        sleep=delays.append,
    )

    with pytest.raises(LLMError, match="gave up"):
        client.complete("hi", model="anthropic/claude-sonnet-5", stage="filter")

    assert len(recorder.requests) == 3
    assert delays == [1.0, 2.0]


def test_400_is_not_retried(tmp_path):
    recorder = Recorder([(400, {"error": {"message": "bad schema"}})])
    client = make_client(recorder, tmp_path)

    with pytest.raises(LLMError, match="400"):
        client.complete("hi", model="anthropic/claude-sonnet-5", stage="filter")

    assert len(recorder.requests) == 1


# -- structured output -------------------------------------------------

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


def test_structured_output_request_and_parse(tmp_path):
    answer = json.dumps({"relevant": True, "reason_code": "breaking_change"})
    recorder = Recorder([(200, chat_payload(answer))])
    client = make_client(recorder, tmp_path)

    result = client.complete(
        "material",
        model="anthropic/claude-sonnet-5",
        stage="filter",
        schema=SCHEMA,
    )

    response_format = recorder.bodies[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "verdict"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["required"] == [
        "relevant",
        "reason_code",
    ]
    assert result.data == {"relevant": True, "reason_code": "breaking_change"}


def test_structured_output_retries_invalid_json(tmp_path):
    good = json.dumps({"relevant": False, "reason_code": "marketing"})
    recorder = Recorder(
        [
            (200, chat_payload("Sure! Here you go: {relevant: maybe}")),
            (200, chat_payload(good)),
        ]
    )
    client = make_client(recorder, tmp_path)

    result = client.complete(
        "material", model="anthropic/claude-sonnet-5", stage="filter", schema=SCHEMA
    )

    assert result.data == {"relevant": False, "reason_code": "marketing"}
    assert result.attempts == 2
    # The retry carries the failure back, otherwise a temperature-0 endpoint
    # would return the identical broken answer.
    retry_messages = recorder.bodies[1]["messages"]
    assert retry_messages[-1]["role"] == "user"
    assert "did not match the required JSON schema" in retry_messages[-1]["content"]


def test_structured_output_retries_schema_violation(tmp_path):
    recorder = Recorder(
        [
            (200, chat_payload(json.dumps({"relevant": True}))),
            (200, chat_payload(json.dumps({"relevant": True, "reason_code": "ok"}))),
        ]
    )
    client = make_client(recorder, tmp_path)

    result = client.complete(
        "material", model="anthropic/claude-sonnet-5", stage="filter", schema=SCHEMA
    )

    assert result.data["reason_code"] == "ok"
    assert len(recorder.requests) == 2


def test_structured_output_gives_up_after_limited_retries(tmp_path):
    recorder = Recorder([(200, chat_payload("not json at all"))])
    client = make_client(recorder, tmp_path, max_schema_retries=1)
    budget = Budget(limit_usd=10.0)

    with pytest.raises(SchemaValidationError):
        client.complete(
            "material",
            model="anthropic/claude-sonnet-5",
            stage="filter",
            schema=SCHEMA,
            budget=budget,
        )

    assert len(recorder.requests) == 2
    # Every retry is a paid call and every one was checked and charged.
    assert budget.spent_usd == pytest.approx(0.008)


def test_structured_output_strips_code_fences(tmp_path):
    fenced = "```json\n" + json.dumps({"relevant": True, "reason_code": "x"}) + "\n```"
    recorder = Recorder([(200, chat_payload(fenced))])
    client = make_client(recorder, tmp_path)

    result = client.complete(
        "material", model="anthropic/claude-sonnet-5", stage="filter", schema=SCHEMA
    )

    assert result.data["reason_code"] == "x"


class Verdict(BaseModel):
    relevant: bool
    reason_code: str = "none"


def test_pydantic_schema_is_accepted(tmp_path):
    recorder = Recorder([(200, chat_payload(json.dumps({"relevant": True})))])
    client = make_client(recorder, tmp_path)

    result = client.complete(
        "material", model="anthropic/claude-sonnet-5", stage="filter", schema=Verdict
    )

    sent = recorder.bodies[0]["response_format"]["json_schema"]
    assert sent["name"] == "Verdict"
    assert sent["schema"]["additionalProperties"] is False
    assert result.data == {"relevant": True, "reason_code": "none"}


def test_structured_result_is_cached_with_its_parse(tmp_path):
    answer = json.dumps({"relevant": True, "reason_code": "pricing"})
    recorder = Recorder([(200, chat_payload(answer))])
    client = make_client(recorder, tmp_path)
    args = {"model": "anthropic/claude-sonnet-5", "stage": "filter", "schema": SCHEMA}

    client.complete("material", **args)
    second = client.complete("material", **args)

    assert len(recorder.requests) == 1
    assert second.cached is True
    assert second.data == {"relevant": True, "reason_code": "pricing"}


# -- missing key -------------------------------------------------------


def test_missing_api_key_is_explicit(tmp_path):
    recorder = Recorder([(200, chat_payload())])
    client = make_client(recorder, tmp_path, api_key=None)

    with pytest.raises(MissingAPIKey, match=API_KEY_ENV):
        client.complete("hi", model="anthropic/claude-sonnet-5", stage="filter")

    assert recorder.requests == []


def test_key_is_picked_up_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "sk-or-live")
    recorder = Recorder([(200, chat_payload())])
    client = make_client(recorder, tmp_path, api_key=None)

    client.complete("hi", model="anthropic/claude-sonnet-5", stage="filter")

    assert recorder.requests[0].headers["authorization"] == "Bearer sk-or-live"


# -- stage routing -----------------------------------------------------


def test_stage_models_defaults():
    models = stage_models()
    assert models["cluster"] == "anthropic/claude-haiku-4.5"
    assert models["filter"] == "anthropic/claude-sonnet-5"
    assert models["enrich"] == "anthropic/claude-opus-5"
    assert models["trends"] == "anthropic/claude-opus-5"
    assert models["score"] is None
    assert DEFAULT_STAGE_MODELS["score"] is None


def test_stage_models_from_config():
    config = {
        "models": {
            "stages": {
                "cluster": "openai/gpt-5-mini",
                "enrich": "google/gemini-3-pro",
                "filter": None,
            }
        }
    }
    models = stage_models(config)

    assert models["cluster"] == "openai/gpt-5-mini"
    assert models["enrich"] == "google/gemini-3-pro"
    assert models["filter"] is None
    # Untouched stages keep their default.
    assert models["trends"] == "anthropic/claude-opus-5"
    assert model_for_stage("cluster", config) == "openai/gpt-5-mini"


def test_stage_models_flat_config_shape():
    config = {"models": {"score": "anthropic/claude-sonnet-5"}}
    assert model_for_stage("score", config) == "anthropic/claude-sonnet-5"


def test_embedding_model_from_config():
    assert embedding_model() == "openai/text-embedding-3-small"
    assert (
        embedding_model({"models": {"embedding_model": "qwen/qwen3-embedding-8b"}})
        == "qwen/qwen3-embedding-8b"
    )


# -- embeddings --------------------------------------------------------


def embeddings_payload(vectors, cost=0.0002):
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": v, "index": i}
            for i, v in enumerate(vectors)
        ],
        "model": "openai/text-embedding-3-small",
        "usage": {"prompt_tokens": 8, "total_tokens": 8, "cost": cost},
    }


def test_embed_calls_the_embeddings_endpoint(tmp_path, run_log):
    recorder = Recorder([(200, embeddings_payload([[0.1, 0.2], [0.3, 0.4]]))])
    client = make_client(recorder, tmp_path)
    budget = Budget(limit_usd=1.0)

    result = client.embed(["a", "b"], run_log=run_log, budget=budget)

    assert str(recorder.requests[0].url).endswith("/embeddings")
    assert recorder.bodies[0]["input"] == ["a", "b"]
    assert recorder.bodies[0]["provider"]["allow_fallbacks"] is False
    assert recorder.bodies[0]["provider"]["order"] == ["openai"]
    assert result.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert result.cost_usd == pytest.approx(0.0002)
    assert budget.spent_usd == pytest.approx(0.0002)
    assert model_call_rows(run_log)[0]["cost_usd"] == pytest.approx(0.0002)


def test_embed_caches_per_text(tmp_path):
    recorder = Recorder(
        [
            (200, embeddings_payload([[0.1], [0.2]])),
            (200, embeddings_payload([[0.3]])),
        ]
    )
    client = make_client(recorder, tmp_path)

    client.embed(["a", "b"])
    second = client.embed(["a", "b", "c"])

    # Only the new text is sent the second time.
    assert recorder.bodies[1]["input"] == ["c"]
    assert second.vectors == [[0.1], [0.2], [0.3]]
    assert second.cache_hits == 2
    assert second.cache_misses == 1


def test_embed_full_cache_hit_spends_nothing(tmp_path):
    recorder = Recorder([(200, embeddings_payload([[0.1]]))])
    client = make_client(recorder, tmp_path)
    budget = Budget(limit_usd=1.0)

    client.embed(["a"], budget=budget)
    spent = budget.spent_usd
    again = client.embed(["a"], budget=budget)

    assert len(recorder.requests) == 1
    assert budget.spent_usd == spent
    assert again.cost_usd == 0.0
    assert again.cache_hits == 1


def test_embed_checks_budget_before_calling(tmp_path):
    recorder = Recorder([(200, embeddings_payload([[0.1]]))])
    client = make_client(recorder, tmp_path)
    budget = Budget(limit_usd=0.001)
    budget.charge(0.001)

    with pytest.raises(BudgetExceeded):
        client.embed(["a"], budget=budget, estimated_usd=0.001)

    assert recorder.requests == []
