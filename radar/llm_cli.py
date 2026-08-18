"""Model calls through the local Claude Code CLI (`claude -p`).

A second backend behind the same surface as `radar.llm.OpenRouterClient`:
same `complete(...)` signature, same `Completion`, same `ModelCache`,
`RunLog.model_call(...)` and `Budget` behaviour. Swapping backends is swapping
the object, and the pipeline stays untouched. It exists so a run can happen on
a machine that has Claude Code but no OpenRouter key.

Three things this backend has to get right:

* **A stable system prompt per stage.** The CLI bills the system prompt through
  Anthropic's prompt cache. Measured on haiku: a first call writes ~14.5k cache
  tokens for $0.033, every later call with the *identical* system prompt reads
  them back for $0.005. Vary one byte and the run pays the write price again,
  six times over. So nothing changeable goes into `--system-prompt`; the
  per-call text belongs in the prompt on stdin, and a system prompt that
  changes inside one stage is written to the run log as a warning.
* **A cache key that the OpenRouter backend also computes.** The key is
  `ModelCache.key_for(model, prompt, schema, params)` with the OpenRouter slug
  and the same `params` dict that `radar.llm` builds, so a cache warmed by
  either backend is read by the other. The nominal `temperature`/`seed` in
  those params describe the request, not what the CLI can enforce.
* **A budget checked before the process starts.** `Budget.check()` runs before
  `subprocess.run`, `Budget.charge()` after, with the `total_cost_usd` the CLI
  reports. `--max-budget-usd` goes along as a second fence in case the process
  outlives our accounting.

**Determinism is weaker here than on the API backend.** The CLI exposes no
`temperature`, no `top_p` and no `seed`, so two identical calls can differ and
nothing in the request can stop that. The call cache is therefore not only a
cost saving on this backend: replaying a run from cache is the only way to get
the same answers twice. There is also no native schema enforcement, so a schema
mismatch is retried with the validation error appended to the prompt — a normal
path, not a safety net.

Verified against CLI 2.1.233. The flags below are the shape that keeps the
per-call overhead down: no tools, no MCP servers, no settings files, one turn,
and the per-machine sections moved out of the system prompt so the prompt cache
is reusable. The binary is executed directly with `shell=False`, so the user's
zsh `claude` wrapper (which injects `-p` and `--max-budget-usd` of its own)
never applies.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

from radar.cache import ModelCache, digest
from radar.llm import (
    API_KEY_ENV,
    Completion,
    EmbeddingsNotSupported,
    LLMError,
    OpenRouterClient,
    SchemaValidationError,
)

# Private on purpose in radar.llm, imported rather than copied: the cache key
# and the schema handling have to stay bit-identical across both backends, and
# a second copy would drift.
from radar.llm import _completion_from_cache, _resolve_schema
from radar.runlog import Budget, RunLog, RunLogLike

PROVIDER = "claude-cli"

# Overrides `shutil.which("claude")`, for a pinned version or a test double.
CLI_BIN_ENV = "RADAR_CLAUDE_CLI"

# Set to 1 to let the integration test really call the CLI.
INTEGRATION_ENV = "RADAR_CLI_INTEGRATION"

EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

# Must not carry a date, a path or a run id: see the module docstring.
DEFAULT_SYSTEM_PROMPT = (
    "You are a batch worker inside a data pipeline. Answer the request "
    "directly, with no preamble and no questions back."
)

BACKEND_OPENROUTER = "openrouter"
BACKEND_CLI = "claude-cli"

_BACKEND_NAMES = {
    "openrouter": BACKEND_OPENROUTER,
    "open-router": BACKEND_OPENROUTER,
    "api": BACKEND_OPENROUTER,
    "cli": BACKEND_CLI,
    "claude": BACKEND_CLI,
    "claude-cli": BACKEND_CLI,
    "claude_cli": BACKEND_CLI,
}

# OpenRouter slug to what `--model` accepts. Short aliases track the current
# model of a family, so a slug pinned to an older version maps to its full CLI
# identifier instead. Unknown slugs raise: guessing here would silently run a
# stage on a model nobody chose.
MODEL_ALIASES: dict[str, str] = {
    "anthropic/claude-fable-5": "fable",
    "anthropic/claude-opus-5": "opus",
    "anthropic/claude-sonnet-5": "sonnet",
    "anthropic/claude-haiku-4.5": "haiku",
    "anthropic/claude-opus-4.8": "claude-opus-4-8",
    "anthropic/claude-opus-4.7": "claude-opus-4-7",
    "anthropic/claude-opus-4.6": "claude-opus-4-6",
    "anthropic/claude-sonnet-4.6": "claude-sonnet-4-6",
}

_CLI_NATIVE_ALIASES = frozenset({"fable", "opus", "sonnet", "haiku"})


class ClaudeCLIError(LLMError):
    """The CLI ran and failed, or answered with something unusable."""


class ClaudeCLINotFound(LLMError):
    pass


class ClaudeCLITimeout(ClaudeCLIError):
    pass


class UnknownModel(LLMError):
    pass


def cli_model(slug: str) -> str:
    """OpenRouter slug to the `--model` value."""
    name = slug.strip()
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    lowered = name.lower()
    if lowered in _CLI_NATIVE_ALIASES:
        return lowered
    if lowered.startswith("claude-"):
        # Already a full CLI identifier.
        return name
    raise UnknownModel(
        f"no Claude CLI model for {slug!r}. Add it to "
        f"radar.llm_cli.MODEL_ALIASES, or configure a slug from "
        f"{sorted(MODEL_ALIASES)}"
    )


class ClaudeCLIClient:
    def __init__(
        self,
        binary: str | None = None,
        *,
        cache: ModelCache | None = None,
        timeout: float = 240.0,
        max_schema_retries: int = 2,
        default_estimate_usd: float = 0.04,
        max_call_usd: float | None = None,
        seed: int | None = 7,
        schema_strict: bool = True,
        cwd: str | None = None,
        thinking_tokens: int | None = 0,
    ) -> None:
        # Resolved per call, not here: a fully cached replay has to run on a
        # machine with no CLI at all.
        self._binary = binary
        self.cache = cache
        self.timeout = timeout
        self.max_schema_retries = max_schema_retries
        self.default_estimate_usd = default_estimate_usd
        self.max_call_usd = max_call_usd
        self.seed = seed
        self.schema_strict = schema_strict
        self.cwd = cwd
        # None leaves the CLI default; 0 turns extended thinking off.
        self.thinking_tokens = thinking_tokens
        # stage -> digest of the system prompt it last used.
        self._system_seen: dict[str, str] = {}

    # -- public API ----------------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        stage: str,
        schema: Any = None,
        system: str | None = None,
        cache_prefix: str | None = None,
        run_log: RunLogLike | None = None,
        budget: Budget | None = None,
        provider: str | None = None,
        max_tokens: int | None = None,
        estimated_usd: float | None = None,
    ) -> Completion:
        """One model call, same contract as `OpenRouterClient.complete`."""
        json_schema, validator = _resolve_schema(schema, self.schema_strict)
        params = _cache_params(
            model=model,
            system=system,
            cache_prefix=cache_prefix,
            provider=provider,
            max_tokens=max_tokens,
            seed=self.seed,
            strict=self.schema_strict,
        )

        key = None
        if self.cache is not None:
            key = ModelCache.key_for(model, prompt, json_schema, params)
            hit = self.cache.get(key)
            if hit is not None:
                completion = _completion_from_cache(hit)
                self._log(run_log, stage, completion)
                return completion

        binary = self._binary_or_raise()
        model_arg = cli_model(model)
        system_prompt = _system_prompt(system, cache_prefix)
        self._check_prompt_stability(run_log, stage, system_prompt)

        ask = prompt if json_schema is None else _with_schema(prompt, json_schema)
        estimate = self.default_estimate_usd if estimated_usd is None else estimated_usd
        last_error: Exception | None = None

        for attempt in range(self.max_schema_retries + 1):
            # Every attempt is a paid call, so every attempt is checked.
            if budget is not None:
                budget.check(estimate)

            payload = self._invoke(binary, model_arg, system_prompt, ask, budget)
            completion = _completion_from_payload(payload, model)
            completion.attempts = attempt + 1

            if budget is not None:
                budget.charge(completion.cost_usd)
            if run_log is not None and payload.get("total_cost_usd") is None:
                run_log.note(
                    f"{stage}: claude CLI reported no total_cost_usd for "
                    f"{model}; run cost is understated"
                )

            if json_schema is None:
                self._store(key, completion)
                self._log(run_log, stage, completion)
                return completion

            try:
                completion.data = _parse_structured(completion.text, validator)
            except SchemaValidationError as exc:
                last_error = exc
                self._log(run_log, stage, completion)
                if attempt >= self.max_schema_retries:
                    break
                # The error goes back with the prompt. Resending the same text
                # to a backend we cannot pin to temperature 0 mostly returns
                # the same broken answer.
                ask = (
                    f"{ask}\n\nYour previous answer did not match the required "
                    f"JSON schema: {exc}\nPrevious answer:\n{completion.text}\n"
                    "Reply with the corrected JSON object only, no prose and "
                    "no code fences."
                )
                continue

            self._store(key, completion)
            self._log(run_log, stage, completion)
            return completion

        raise SchemaValidationError(
            f"{stage}: {model} failed to satisfy the schema after "
            f"{self.max_schema_retries + 1} attempts: {last_error}"
        )

    def embed(self, *args: Any, **kwargs: Any) -> Any:
        raise EmbeddingsNotSupported(
            "the Claude CLI backend cannot embed; keep an OpenRouter client "
            "(or another embedding provider) for radar.llm.embedding_model"
        )

    def close(self) -> None:
        """Nothing to close: each call is its own process."""

    # -- internals -----------------------------------------------------

    def _binary_or_raise(self) -> str:
        binary = (
            self._binary
            or os.environ.get(CLI_BIN_ENV, "").strip()
            or shutil.which("claude")
        )
        if not binary:
            raise ClaudeCLINotFound(
                "claude CLI not found on PATH. Install Claude Code or point "
                f"{CLI_BIN_ENV} at the binary. Only calls already in the "
                "model cache can run without it."
            )
        return binary

    def _budget_cap(self, budget: Budget | None) -> float | None:
        if budget is not None:
            return budget.remaining_usd
        return self.max_call_usd

    def argv(
        self,
        binary: str,
        model_arg: str,
        system_prompt: str,
        budget: Budget | None = None,
    ) -> list[str]:
        cap = self._budget_cap(budget)
        argv = [
            binary,
            "-p",
            "--model",
            model_arg,
            "--output-format",
            "json",
            "--max-turns",
            "1",
            "--dangerously-skip-permissions",
            # Everything below trims the per-call overhead: no tools, no MCP,
            # no user/project settings, and the per-machine system prompt
            # sections moved into the first user message so the prompt cache
            # is shared across calls.
            "--allowed-tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            EMPTY_MCP_CONFIG,
            "--setting-sources",
            "",
            "--system-prompt",
            system_prompt,
            "--exclude-dynamic-system-prompt-sections",
        ]
        if cap is not None:
            argv += ["--max-budget-usd", f"{cap:.4f}"]
        return argv

    def _child_env(self) -> dict[str, str]:
        """Environment for the CLI child.

        Measured over a full run: 424k output tokens paid for, 74k of JSON
        parsed. The missing 82 percent is extended thinking, which extraction
        does not need — it returns structured events, it does not reason its
        way to them. On the same material with thinking off: 6 seconds instead
        of 16, half the cost, the same four events and the same three dates,
        losing only incidental facts that never reach a card.

        It also removes the second defect: the largest legitimate call ran 165
        seconds against a 180 second timeout, so seven materials were cut off
        mid-answer. Short answers do not reach the ceiling.
        """
        env = dict(os.environ)
        if self.thinking_tokens is not None:
            env["MAX_THINKING_TOKENS"] = str(self.thinking_tokens)
        return env

    def _invoke(
        self,
        binary: str,
        model_arg: str,
        system_prompt: str,
        prompt: str,
        budget: Budget | None,
    ) -> dict[str, Any]:
        argv = self.argv(binary, model_arg, system_prompt, budget)
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, shell=False
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                shell=False,
                cwd=self.cwd,
                env=self._child_env(),
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run kills and reaps the child before re-raising, so a
            # wedged call cannot keep a backfill waiting forever.
            raise ClaudeCLITimeout(
                f"claude CLI did not answer within {self.timeout:.0f}s for "
                f"model {model_arg}"
            ) from exc
        except FileNotFoundError as exc:
            raise ClaudeCLINotFound(f"cannot execute {binary}: {exc}") from exc

        if proc.returncode != 0:
            raise ClaudeCLIError(
                f"claude CLI exited {proc.returncode} for model {model_arg}: "
                f"{_tail(proc.stderr)}"
            )
        try:
            payload = json.loads(proc.stdout or "")
        except json.JSONDecodeError as exc:
            raise ClaudeCLIError(
                f"claude CLI did not return JSON ({exc}): {_tail(proc.stdout)}"
            ) from exc
        if not isinstance(payload, dict):
            raise ClaudeCLIError(
                f"claude CLI returned {type(payload).__name__}, expected an object"
            )
        if payload.get("is_error"):
            raise ClaudeCLIError(
                f"claude CLI reported an error for model {model_arg}: "
                f"{payload.get('result') or payload.get('subtype') or payload}"
            )
        return payload

    def _check_prompt_stability(
        self, run_log: RunLogLike | None, stage: str, system_prompt: str
    ) -> None:
        """A stage that changes its system prompt pays six times the price."""
        fingerprint = digest(system_prompt)
        previous = self._system_seen.get(stage)
        self._system_seen[stage] = fingerprint
        if previous is not None and previous != fingerprint and run_log is not None:
            run_log.note(
                f"{stage}: the claude-cli system prompt changed between calls; "
                "the prompt cache will miss and the stage costs several times more"
            )

    def _store(self, key: str | None, completion: Completion) -> None:
        if key is None or self.cache is None:
            return
        self.cache.put(
            key,
            {
                # Same shape radar.llm writes and reads, so either backend can
                # replay the other's entries.
                "kind": "completion",
                "text": completion.text,
                "data": completion.data,
                "cost_usd": completion.cost_usd,
                "tokens_in": completion.tokens_in,
                "tokens_out": completion.tokens_out,
                "cached_tokens": completion.cached_tokens,
                "model": completion.model,
                "provider": completion.provider,
                "finish_reason": completion.finish_reason,
                "cache_creation_tokens": int(
                    (completion.raw.get("usage") or {}).get(
                        "cache_creation_input_tokens"
                    )
                    or 0
                ),
            },
        )

    @staticmethod
    def _log(run_log: RunLogLike | None, stage: str, completion: Completion) -> None:
        if run_log is None:
            return
        run_log.model_call(
            stage=stage,
            model=completion.model,
            provider=completion.provider,
            tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out,
            cost_usd=completion.cost_usd,
            cached=completion.cached,
            # A hit costs nothing and would have cost this. The figure existed
            # on Completion and reached no table, so a run served from cache
            # reported the price of the cache as the price of the work.
            original_cost_usd=completion.original_cost_usd or completion.cost_usd,
        )


# -- backend selection -------------------------------------------------


def make_backend(
    config: Any = None,
    prefer: str | None = None,
    *,
    cache: ModelCache | None = None,
    run_log: RunLogLike | None = None,
    **kwargs: Any,
) -> OpenRouterClient | ClaudeCLIClient:
    """Pick a model backend and say so in the log.

    `prefer` wins, then `models.backend` in the theme config, then the
    environment: a key means OpenRouter, no key means the CLI. Extra keyword
    arguments go to whichever client is built.

    `llm.max_output_tokens` is passed on. It carries an incident in its own
    comment — a run that produced 424 thousand output tokens — and it reached
    no client: the ceiling recorded against that incident was never in force.
    An explicit keyword still wins, so callers that know better are unaffected.
    """
    section = {}
    if config is not None:
        getter = getattr(config, "section", None)
        section = getter("llm") if callable(getter) else (config.get("llm") or {})
    ceiling = section.get("max_output_tokens")
    if ceiling and "max_tokens" not in kwargs:
        try:
            kwargs["max_tokens"] = int(ceiling)
        except (TypeError, ValueError):
            pass

    if prefer is not None:
        choice, reason = _backend_name(prefer), "explicit preference"
    else:
        configured = _configured_backend(config)
        if configured is not None:
            choice, reason = _backend_name(configured), "models.backend in the config"
        elif os.environ.get(API_KEY_ENV, "").strip():
            choice, reason = BACKEND_OPENROUTER, f"{API_KEY_ENV} is set"
        else:
            choice, reason = BACKEND_CLI, f"{API_KEY_ENV} is not set"

    if run_log is not None:
        run_log.note(f"model backend: {choice} ({reason})")

    if choice == BACKEND_OPENROUTER:
        return OpenRouterClient(cache=cache, **kwargs)
    return ClaudeCLIClient(cache=cache, **kwargs)


def _backend_name(value: str) -> str:
    name = _BACKEND_NAMES.get(str(value).strip().lower())
    if name is None:
        raise ValueError(
            f"unknown model backend {value!r}; expected "
            f"{BACKEND_OPENROUTER!r} or {BACKEND_CLI!r}"
        )
    return name


def _configured_backend(config: Any) -> str | None:
    if config is None:
        return None
    section = config.models if hasattr(config, "models") else config.get("models")
    if not isinstance(section, Mapping):
        return None
    value = section.get("backend")
    return str(value).strip() or None if value else None


# -- helpers -----------------------------------------------------------


def _cache_params(
    *,
    model: str,
    system: str | None,
    cache_prefix: str | None,
    provider: str | None,
    max_tokens: int | None,
    seed: int | None,
    strict: bool,
) -> dict[str, Any]:
    """The params component of the cache key, as `radar.llm` builds it.

    Field for field the same dict, including the sampling values the CLI
    cannot actually set: they describe the request the pipeline asked for, and
    keeping them identical is what lets one warmed cache serve both backends.
    """
    return {
        "temperature": 0.0,
        "top_p": 1.0,
        "seed": seed,
        "max_tokens": max_tokens,
        "provider": provider or model.split("/", 1)[0],
        "system": system or "",
        "cache_prefix": cache_prefix or "",
        "strict": strict,
    }


def _system_prompt(system: str | None, cache_prefix: str | None) -> str:
    """Shared prefix first, per-stage system prompt after it."""
    parts = [part for part in (cache_prefix, system) if part]
    return "\n\n".join(parts) if parts else DEFAULT_SYSTEM_PROMPT


def _with_schema(prompt: str, json_schema: Mapping[str, Any]) -> str:
    """Schema goes in the prompt, not the system prompt.

    The CLI has no `response_format`, so the contract has to be stated in
    words; putting it on stdin keeps the system prompt stable across stages
    that share one prompt but not one schema.
    """
    return (
        f"{prompt}\n\nAnswer with a single JSON object that validates against "
        "this JSON Schema. No prose, no code fences.\n"
        f"{json.dumps(json_schema, sort_keys=True, ensure_ascii=False)}"
    )


def _tail(text: str | None, limit: int = 300) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return "<no output>"
    return cleaned[-limit:]


def _completion_from_payload(payload: Mapping[str, Any], model: str) -> Completion:
    text = payload.get("result")
    if not isinstance(text, str):
        raise ClaudeCLIError(
            f"claude CLI returned no result string for {model}: {payload}"
        )
    usage = payload.get("usage") or {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    cost = float(payload.get("total_cost_usd") or 0.0)
    return Completion(
        text=text,
        cost_usd=cost,
        original_cost_usd=cost,
        # Cache tokens are almost the whole bill on this backend, so they are
        # part of tokens_in; cached_tokens keeps the read share visible on its
        # own, and the write share stays in `raw`.
        tokens_in=int(usage.get("input_tokens") or 0) + cache_read + cache_write,
        tokens_out=int(usage.get("output_tokens") or 0),
        cached_tokens=cache_read,
        # The OpenRouter slug, not the canonical CLI name, so rows from the two
        # backends line up in the log; the canonical name stays in `raw`.
        model=model,
        provider=PROVIDER,
        cached=False,
        finish_reason=payload.get("stop_reason") or payload.get("subtype"),
        raw=dict(payload),
    )


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -len("```")]
    return body.strip()


def _first_json_block(text: str) -> str | None:
    """First balanced object or array, strings and escapes respected.

    Needed because the CLI answer often carries a sentence in front of the
    JSON, and a naive slice on the first and last brace eats a trailing brace
    inside a string.
    """
    openers = {"{": "}", "[": "]"}
    start = next((i for i, ch in enumerate(text) if ch in openers), None)
    if start is None:
        return None
    opener = text[start]
    closer = openers[opener]
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _loads(candidate: str | None) -> Any:
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _parse_structured(text: str, validator: Callable[[Any], Any] | None) -> Any:
    unfenced = _strip_fences(text)
    parsed = _loads(unfenced)
    if parsed is None:
        parsed = _loads(_first_json_block(unfenced))
    if parsed is None:
        raise SchemaValidationError(
            f"response contains no JSON object: {text.strip()[:200]!r}"
        )
    if validator is None:
        return parsed
    try:
        return validator(parsed)
    except SchemaValidationError:
        raise
    except Exception as exc:  # pydantic ValidationError and friends
        raise SchemaValidationError(str(exc)) from exc


__all__ = [
    "BACKEND_CLI",
    "BACKEND_OPENROUTER",
    "CLI_BIN_ENV",
    "ClaudeCLIClient",
    "ClaudeCLIError",
    "ClaudeCLINotFound",
    "ClaudeCLITimeout",
    "Completion",
    "INTEGRATION_ENV",
    "MODEL_ALIASES",
    "PROVIDER",
    "UnknownModel",
    "cli_model",
    "make_backend",
]
