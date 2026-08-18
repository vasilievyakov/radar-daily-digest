"""OpenRouter client for every model call in the pipeline.

Three properties are not optional here, because without them a run cannot be
compared with the run before it:

* **Determinism.** `temperature = 0`, one provider pinned, `allow_fallbacks`
  off. A bare model slug is routed to whichever endpoint is cheapest at that
  second, and endpoints differ in quantization, so the nightly run and the
  morning rerun disagree for reasons nothing in the log explains. The provider
  that actually served the call is recorded on every row.
* **Caching by input hash.** A call is keyed by model, prompt, schema and
  parameters. A cache hit costs nothing and is marked `cached` in the run log,
  which is what makes a backfill replayable in seconds instead of dollars.
* **Budget checked before the request.** `Budget.check()` runs before the
  socket opens and `Budget.charge()` after the response lands, with the actual
  `usage.cost` OpenRouter reports. A ceiling noticed afterwards in the log has
  not limited anything.

API details verified against the live docs (August 2026):

* `POST https://openrouter.ai/api/v1/chat/completions`, bearer auth.
* Usage accounting is always on: `usage.cost` is the amount charged, and
  `usage.cost_details.upstream_inference_cost` what the upstream charged. The
  `usage: {include: true}` request field is deprecated and has no effect.
* Provider identity is no longer a top-level response field. It arrives in
  `openrouter_metadata` and only when the request carries
  `X-OpenRouter-Metadata: enabled`, under
  `endpoints.available[].provider` for the entry with `selected: true`.
* Prompt caching is a `cache_control: {"type": "ephemeral"}` marker on a text
  content block; hits come back as `usage.prompt_tokens_details.cached_tokens`.
* Structured output is `response_format.json_schema` with `name`, `strict`,
  `schema`. Enforcement varies by provider, so parse failures are retried.
* Embeddings exist: `POST /api/v1/embeddings`, OpenAI-shaped, and the response
  `usage` carries `cost` like chat does.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from radar.cache import ModelCache
from radar.runlog import Budget, RunLog

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
API_KEY_ENV = "OPENROUTER_API_KEY"

APP_URL = "https://github.com/vasilievyakov/radar-daily-digest"
APP_TITLE = "radar-daily-digest"

# Stage to model, defaults only. The real mapping comes from the theme config
# (CFG-1): a new domain must not require touching a stage's code.
DEFAULT_STAGE_MODELS: dict[str, str | None] = {
    "collect": None,  # code is enough
    "cluster": "anthropic/claude-haiku-4.5",  # mechanical similarity, high volume
    "filter": "anthropic/claude-sonnet-5",  # runs over every material
    "enrich": "anthropic/claude-opus-5",  # a wrong date is published outward
    "delta": None,  # deterministic matching (FR-5.1)
    "trends": "anthropic/claude-opus-5",  # claims must survive FR-5.8
    "score": None,  # weighted arithmetic from the config (FR-6.3)
    "publish": None,
}

DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"

# 408 and 429 are the throttles, 5xx the upstream hiccups. Everything else is
# our own bad request and retrying it just spends the budget twice.
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 520, 522, 524, 529})


class LLMError(RuntimeError):
    """Any failure of a model call that the caller has to see."""


class MissingAPIKey(LLMError):
    pass


class SchemaValidationError(LLMError):
    pass


class EmbeddingsNotSupported(LLMError):
    pass


@dataclass(slots=True)
class Completion:
    """One model call, as the pipeline and the run log see it."""

    text: str
    data: Any = None
    # What this call charged now: zero on a cache hit, by definition.
    cost_usd: float = 0.0
    # What the call cost when it was really made, kept so a cached run can
    # still report what it would have spent.
    original_cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    model: str = ""
    provider: str | None = None
    cached: bool = False
    finish_reason: str | None = None
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str = ""
    cost_usd: float = 0.0
    original_cost_usd: float = 0.0
    tokens_in: int = 0
    provider: str | None = None
    cache_hits: int = 0
    cache_misses: int = 0


def stage_models(config: Mapping[str, Any] | None = None) -> dict[str, str | None]:
    """Stage to model slug, config over defaults.

    Accepts either `{"models": {"cluster": ...}}` or
    `{"models": {"stages": {"cluster": ...}}}`; an explicit null, empty string
    or "none" means the stage runs without a model at all.
    """
    merged = dict(DEFAULT_STAGE_MODELS)
    section = (config or {}).get("models") or {}
    stages = (
        section.get("stages") if isinstance(section.get("stages"), Mapping) else section
    )
    for stage, slug in (stages or {}).items():
        if stage in (
            "stages",
            "provider",
            "providers",
            "embeddings",
            "embedding_model",
        ):
            continue
        merged[str(stage)] = None if _is_blank(slug) else str(slug)
    return merged


def model_for_stage(stage: str, config: Mapping[str, Any] | None = None) -> str | None:
    return stage_models(config).get(stage)


def embedding_model(config: Mapping[str, Any] | None = None) -> str:
    section = (config or {}).get("models") or {}
    slug = section.get("embedding_model") or section.get("embeddings")
    return DEFAULT_EMBEDDING_MODEL if _is_blank(slug) else str(slug)


def _is_blank(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and value.strip().lower() in ("", "none", "-", "null")
    )


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        cache: ModelCache | None = None,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        provider: str | None = None,
        provider_overrides: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        max_retries: int = 4,
        max_schema_retries: int = 2,
        default_estimate_usd: float = 0.02,
        max_tokens: int | None = None,
        seed: int | None = 7,
        schema_strict: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Resolved lazily rather than here: a fully cached replay of a backfill
        # must run on a machine that has no key at all.
        self._api_key = api_key
        self.cache = cache
        self.base_url = base_url.rstrip("/")
        self.provider = provider
        self.provider_overrides = dict(provider_overrides or {})
        self.max_retries = max_retries
        self.max_schema_retries = max_schema_retries
        self.default_estimate_usd = default_estimate_usd
        self.max_tokens = max_tokens
        self.seed = seed
        self.schema_strict = schema_strict
        self._sleep = sleep
        self._client = client or httpx.Client(timeout=timeout)

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
        run_log: RunLog | None = None,
        budget: Budget | None = None,
        provider: str | None = None,
        max_tokens: int | None = None,
        estimated_usd: float | None = None,
    ) -> Completion:
        """One model call. See the module docstring for the invariants."""
        json_schema, validator = _resolve_schema(schema, self.schema_strict)
        provider_slug = self._provider_for(model, provider)
        params = {
            "temperature": 0.0,
            "top_p": 1.0,
            "seed": self.seed,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "provider": provider_slug,
            "system": system or "",
            "cache_prefix": cache_prefix or "",
            "strict": self.schema_strict,
        }

        key = None
        if self.cache is not None:
            key = ModelCache.key_for(model, prompt, json_schema, params)
            hit = self.cache.get(key)
            if hit is not None:
                completion = _completion_from_cache(hit)
                self._log(run_log, stage, completion)
                return completion

        body = self._build_body(
            prompt=prompt,
            model=model,
            system=system,
            cache_prefix=cache_prefix,
            json_schema=json_schema,
            params=params,
        )

        estimate = self.default_estimate_usd if estimated_usd is None else estimated_usd
        messages = list(body["messages"])
        last_error: Exception | None = None

        for attempt in range(self.max_schema_retries + 1):
            body["messages"] = messages
            # Checked on every attempt: a schema retry is a second paid call.
            if budget is not None:
                budget.check(estimate)

            payload = self._post("/chat/completions", body)
            completion = _completion_from_payload(payload, model)
            completion.attempts = attempt + 1

            if budget is not None:
                budget.charge(completion.cost_usd)
            if run_log is not None and payload.get("usage", {}).get("cost") is None:
                run_log.note(
                    f"{stage}: OpenRouter returned no usage.cost for {model}; "
                    "run cost is understated"
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
                # Feed the failure back rather than resending the same prompt:
                # an identical request to a temperature-0 endpoint returns the
                # same broken answer.
                messages = [
                    *messages,
                    {"role": "assistant", "content": completion.text},
                    {
                        "role": "user",
                        "content": (
                            "Your previous answer did not match the required JSON "
                            f"schema: {exc}. Reply with the corrected JSON object "
                            "only, no prose and no code fences."
                        ),
                    },
                ]
                continue

            self._store(key, completion)
            self._log(run_log, stage, completion)
            return completion

        raise SchemaValidationError(
            f"{stage}: {model} failed to satisfy the schema after "
            f"{self.max_schema_retries + 1} attempts: {last_error}"
        )

    def embed(
        self,
        texts: Sequence[str] | str,
        model: str = DEFAULT_EMBEDDING_MODEL,
        *,
        stage: str = "embed",
        run_log: RunLog | None = None,
        budget: Budget | None = None,
        provider: str | None = None,
        dimensions: int | None = None,
        estimated_usd: float | None = None,
    ) -> EmbeddingResult:
        """Embeddings through OpenRouter's OpenAI-shaped `/embeddings`.

        Cached per text, not per batch: the corpus grows by a few statements a
        day and a batch-level key would re-pay for the whole corpus each time.
        Batch cost is split across the texts that were actually sent.
        """
        items = [texts] if isinstance(texts, str) else list(texts)
        if not items:
            return EmbeddingResult(vectors=[], model=model)

        provider_slug = self._provider_for(model, provider)
        params = {"dimensions": dimensions, "provider": provider_slug}

        vectors: list[list[float]] = [[] for _ in items]
        keys: list[str | None] = [None] * len(items)
        pending: list[int] = []
        hits = 0
        for index, text in enumerate(items):
            if self.cache is not None:
                key = ModelCache.key_for(model, text, None, params)
                keys[index] = key
                cached = self.cache.get(key)
                if cached is not None:
                    vectors[index] = list(cached["embedding"])
                    hits += 1
                    continue
            pending.append(index)

        result = EmbeddingResult(
            vectors=vectors,
            model=model,
            provider=provider_slug,
            cache_hits=hits,
            cache_misses=len(pending),
        )
        if hits and run_log is not None:
            run_log.model_call(
                stage=stage, model=model, provider=provider_slug, cached=True
            )
        if not pending:
            return result

        estimate = self.default_estimate_usd if estimated_usd is None else estimated_usd
        if budget is not None:
            budget.check(estimate)

        body: dict[str, Any] = {
            "model": model,
            "input": [items[i] for i in pending],
            "encoding_format": "float",
            "provider": self._provider_block(provider_slug),
        }
        if dimensions is not None:
            body["dimensions"] = dimensions

        payload = self._post("/embeddings", body)
        usage = payload.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
        tokens_in = int(usage.get("prompt_tokens") or 0)
        served = payload.get("data") or []
        if len(served) != len(pending):
            raise LLMError(
                f"embeddings: asked for {len(pending)} vectors, got {len(served)}"
            )

        share = cost / len(pending) if pending else 0.0
        for item in served:
            index = pending[int(item.get("index", 0))]
            vector = [float(x) for x in item["embedding"]]
            vectors[index] = vector
            if self.cache is not None and keys[index]:
                self.cache.put(
                    keys[index],
                    {
                        "kind": "embedding",
                        "model": payload.get("model", model),
                        "embedding": vector,
                        "cost_usd": share,
                        "provider": _provider_of(payload),
                    },
                )

        if budget is not None:
            budget.charge(cost)
        result.cost_usd = cost
        result.original_cost_usd = cost
        result.tokens_in = tokens_in
        result.provider = _provider_of(payload) or provider_slug
        result.model = payload.get("model", model)
        if run_log is not None:
            run_log.model_call(
                stage=stage,
                model=result.model,
                provider=result.provider,
                tokens_in=tokens_in,
                cost_usd=cost,
                cached=False,
                original_cost_usd=cost,
            )
        return result

    def close(self) -> None:
        self._client.close()

    # -- internals -----------------------------------------------------

    def _api_key_or_raise(self) -> str:
        key = self._api_key or os.environ.get(API_KEY_ENV, "")
        if not key.strip():
            raise MissingAPIKey(
                f"{API_KEY_ENV} is not set. Put the OpenRouter key in the "
                f"environment (see .env.example) or pass api_key=. Only calls "
                f"already in the model cache can run without it."
            )
        return key.strip()

    def _provider_for(self, model: str, override: str | None) -> str:
        if override:
            return override
        if model in self.provider_overrides:
            return self.provider_overrides[model]
        if self.provider:
            return self.provider
        # First-party slugs carry their provider: "anthropic/claude-opus-5" is
        # served by the "anthropic" endpoint. Anything else must be pinned in
        # the config, and pinning the wrong slug fails loudly rather than
        # silently drifting to another endpoint.
        return model.split("/", 1)[0]

    def _provider_block(self, provider_slug: str) -> dict[str, Any]:
        return {
            # order + only + allow_fallbacks: one endpoint, no substitution.
            "order": [provider_slug],
            "only": [provider_slug],
            "allow_fallbacks": False,
            # If the pinned endpoint cannot honour a parameter we sent, we want
            # an error, not a quietly dropped response_format.
            "require_parameters": True,
        }

    def _build_body(
        self,
        *,
        prompt: str,
        model: str,
        system: str | None,
        cache_prefix: str | None,
        json_schema: dict[str, Any] | None,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": _build_messages(prompt, system, cache_prefix),
            "temperature": 0.0,
            "top_p": 1.0,
            "provider": self._provider_block(str(params["provider"])),
        }
        if params.get("seed") is not None:
            body["seed"] = params["seed"]
        if params.get("max_tokens") is not None:
            body["max_tokens"] = params["max_tokens"]
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _schema_name(json_schema),
                    "strict": bool(params.get("strict", True)),
                    "schema": json_schema,
                },
            }
        return body

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key_or_raise()}",
            "Content-Type": "application/json",
            "HTTP-Referer": APP_URL,
            "X-OpenRouter-Title": APP_TITLE,
            # Provider identity is opt-in now; without this header the log
            # cannot say which endpoint answered.
            "X-OpenRouter-Metadata": "enabled",
        }
        url = f"{self.base_url}{path}"
        last: str = "unknown error"

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post(url, json=dict(body), headers=headers)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code in RETRY_STATUS:
                    last = f"HTTP {response.status_code}: {response.text[:300]}"
                    if attempt < self.max_retries:
                        self._sleep(
                            _backoff(attempt, response.headers.get("retry-after"))
                        )
                        continue
                    raise LLMError(f"OpenRouter {path} gave up after {last}")
                if response.status_code == 404 and path == "/embeddings":
                    raise EmbeddingsNotSupported(
                        "OpenRouter /embeddings returned 404: the endpoint is gone, "
                        "embeddings need a separate provider"
                    )
                if response.status_code >= 400:
                    raise LLMError(
                        f"OpenRouter {path} HTTP {response.status_code}: "
                        f"{response.text[:500]}"
                    )
                payload = response.json()
                # A 200 can still carry an error envelope.
                if isinstance(payload, dict) and payload.get("error"):
                    raise LLMError(f"OpenRouter {path}: {payload['error']}")
                return payload
            if attempt < self.max_retries:
                self._sleep(_backoff(attempt, None))

        raise LLMError(f"OpenRouter {path} failed: {last}")

    def _store(self, key: str | None, completion: Completion) -> None:
        if key is None or self.cache is None:
            return
        self.cache.put(
            key,
            {
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
            },
        )

    @staticmethod
    def _log(run_log: RunLog | None, stage: str, completion: Completion) -> None:
        if run_log is None:
            return
        # Tokens are logged even on a hit, so the log shows the work the run
        # would have done; cost stays zero because nothing was charged, and
        # the `cached` flag is what tells the two apart.
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


# -- helpers -----------------------------------------------------------


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return float(2**attempt)


def _build_messages(
    prompt: str, system: str | None, cache_prefix: str | None
) -> list[dict[str, Any]]:
    """Shared prefix first, marked cacheable.

    The saving is proportional to the prefix's share of the request, so marking
    the vendor dictionary in front of a 40k-token changelog buys almost
    nothing. It pays on the short, high-volume calls of stages 2 and 3.
    """
    messages: list[dict[str, Any]] = []
    if cache_prefix:
        parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": cache_prefix,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if system:
            parts.append({"type": "text", "text": system})
        messages.append({"role": "system", "content": parts})
    elif system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def _schema_name(schema: Mapping[str, Any]) -> str:
    raw = str(schema.get("title") or "response")
    cleaned = "".join(c if c.isalnum() or c in "_-" else "_" for c in raw)
    return cleaned[:64] or "response"


def _resolve_schema(
    schema: Any, strict: bool
) -> tuple[dict[str, Any] | None, Callable[[Any], Any] | None]:
    """Accept a pydantic model or a raw JSON Schema dict.

    A pydantic model gets a real validator for free, which matters because
    OpenRouter documents that strict enforcement varies by provider.
    """
    if schema is None:
        return None, None
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        json_schema = _strictify(schema.model_json_schema(), strict)
        json_schema.setdefault("title", schema.__name__)

        def validate(instance: Any) -> Any:
            return schema.model_validate(instance).model_dump(mode="json")

        return json_schema, validate
    if isinstance(schema, Mapping):
        json_schema = _strictify(dict(schema), strict)

        def validate(instance: Any) -> Any:
            _validate_json_schema(instance, json_schema)
            return instance

        return json_schema, validate
    raise TypeError(
        f"schema must be a pydantic model or a JSON Schema dict, got {type(schema)}"
    )


def _strictify(node: Any, strict: bool) -> Any:
    if not strict or not isinstance(node, dict):
        return node
    if node.get("type") == "object" and "additionalProperties" not in node:
        node["additionalProperties"] = False
    for key in ("properties", "$defs", "definitions"):
        sub = node.get(key)
        if isinstance(sub, dict):
            for name, value in sub.items():
                sub[name] = _strictify(value, strict)
    for key in ("items", "additionalProperties"):
        if isinstance(node.get(key), dict):
            node[key] = _strictify(node[key], strict)
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(node.get(key), list):
            node[key] = [_strictify(v, strict) for v in node[key]]
    return node


_JSON_TYPES: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _validate_json_schema(
    instance: Any, schema: Mapping[str, Any], path: str = "$"
) -> None:
    """Enough of JSON Schema to catch a model that answered off-contract.

    Deliberately small: the pydantic path covers the real models, this one only
    has to fail loudly on the raw-dict callers.
    """
    expected = schema.get("type")
    if isinstance(expected, str) and expected in _JSON_TYPES:
        if expected == "boolean":
            ok = isinstance(instance, bool)
        elif expected in ("number", "integer"):
            ok = isinstance(instance, _JSON_TYPES[expected]) and not isinstance(
                instance, bool
            )
        else:
            ok = isinstance(instance, _JSON_TYPES[expected])
        if not ok:
            raise SchemaValidationError(
                f"{path}: expected {expected}, got {type(instance).__name__}"
            )
    enum = schema.get("enum")
    if enum is not None and instance not in enum:
        raise SchemaValidationError(f"{path}: {instance!r} is not one of {enum}")
    if isinstance(instance, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in instance:
                raise SchemaValidationError(f"{path}: missing required key {name!r}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(instance) - set(properties))
            if extra:
                raise SchemaValidationError(f"{path}: unexpected keys {extra}")
        for name, sub in properties.items():
            if name in instance and isinstance(sub, Mapping):
                _validate_json_schema(instance[name], sub, f"{path}.{name}")
    if isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for i, item in enumerate(instance):
                _validate_json_schema(item, items, f"{path}[{i}]")


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -len("```")]
    return body.strip()


def _parse_structured(text: str, validator: Callable[[Any], Any] | None) -> Any:
    try:
        parsed = json.loads(_strip_fences(text))
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(f"response is not JSON: {exc}") from exc
    if validator is None:
        return parsed
    try:
        return validator(parsed)
    except ValidationError as exc:
        raise SchemaValidationError(str(exc)) from exc


def _provider_of(payload: Mapping[str, Any]) -> str | None:
    """Which endpoint actually answered.

    `openrouter_metadata` is the documented place and needs the opt-in header;
    the old top-level `provider` field is read as a fallback so a change on
    either side does not leave the log blank.
    """
    meta = payload.get("openrouter_metadata") or {}
    if isinstance(meta, Mapping):
        endpoints = (meta.get("endpoints") or {}).get("available") or []
        for endpoint in endpoints:
            if isinstance(endpoint, Mapping) and endpoint.get("selected"):
                return endpoint.get("provider")
        attempts = meta.get("attempts") or []
        if attempts and isinstance(attempts[-1], Mapping):
            return attempts[-1].get("provider")
    top_level = payload.get("provider")
    return top_level if isinstance(top_level, str) else None


def _completion_from_payload(payload: Mapping[str, Any], model: str) -> Completion:
    choices = payload.get("choices") or []
    if not choices:
        raise LLMError(f"OpenRouter returned no choices for {model}: {payload}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # Some providers answer with content parts rather than a bare string.
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, Mapping)
        )
    usage = payload.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cost = float(usage.get("cost") or 0.0)
    return Completion(
        text=content or "",
        cost_usd=cost,
        original_cost_usd=cost,
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        cached_tokens=int(details.get("cached_tokens") or 0),
        model=str(payload.get("model") or model),
        provider=_provider_of(payload),
        cached=False,
        finish_reason=choices[0].get("finish_reason"),
        raw=dict(payload),
    )


def _completion_from_cache(entry: Mapping[str, Any]) -> Completion:
    return Completion(
        text=entry.get("text", ""),
        data=entry.get("data"),
        cost_usd=0.0,
        original_cost_usd=float(entry.get("cost_usd") or 0.0),
        tokens_in=int(entry.get("tokens_in") or 0),
        tokens_out=int(entry.get("tokens_out") or 0),
        cached_tokens=int(entry.get("cached_tokens") or 0),
        model=str(entry.get("model") or ""),
        provider=entry.get("provider"),
        cached=True,
        finish_reason=entry.get("finish_reason"),
        raw={},
    )


__all__ = [
    "API_KEY_ENV",
    "Budget",
    "Completion",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_STAGE_MODELS",
    "EmbeddingResult",
    "EmbeddingsNotSupported",
    "LLMError",
    "MissingAPIKey",
    "OpenRouterClient",
    "SchemaValidationError",
    "embedding_model",
    "model_for_stage",
    "stage_models",
]
