"""Provider-agnostic LLM client (OpenAI / Anthropic / Gemini over plain REST via httpx).

Why raw REST instead of three SDKs: one small dependency (httpx), identical retry / timeout /
logging behaviour for every provider, and trivial to mock in tests.

Structured output strategy per provider
  - OpenAI    : Chat Completions, response_format=json_object (+ schema example in the prompt)
  - Anthropic : Messages API with a forced tool call whose input_schema is our Pydantic schema
  - Gemini    : generateContent with responseMimeType=application/json
Every reply is then validated with Pydantic; invalid output gets ONE repair attempt that quotes the
validator errors, and finally the optional fallback provider is tried.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, TypeVar

import httpx

from app.config import MODEL_PRICING, Settings
from app.errors import AppError

log = logging.getLogger("link_analysis.llm")
T = TypeVar("T")

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TOOL_NAME = "submit_analysis"


# ---------------------------------------------------------------- data classes / errors
@dataclass
class Completion:
    text: str
    input_tokens: int = 0          # non-cached prompt tokens
    cached_input_tokens: int = 0
    output_tokens: int = 0         # includes reasoning/thinking tokens (they are billed as output)
    latency_ms: int = 0


@dataclass
class LLMUsage:
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    llm_ms: int = 0
    calls: int = 0
    repaired: bool = False
    fallback_used: bool = False
    cost_usd: Optional[float] = None

    def add(self, c: Completion) -> None:
        self.input_tokens += c.input_tokens
        self.cached_input_tokens += c.cached_input_tokens
        self.output_tokens += c.output_tokens
        self.llm_ms += c.latency_ms
        self.calls += 1


class LLMProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None, kind: str = "error"):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.kind = kind


class StructuredOutputError(Exception):
    """The model answered, but not with valid / schema-conformant JSON."""


def estimate_cost(provider: str, model: str, usage: LLMUsage | Completion) -> Optional[float]:
    price = MODEL_PRICING.get((provider, model))
    if not price:
        return None
    p_in, p_out, p_cached = price
    return round((usage.input_tokens * p_in + usage.cached_input_tokens * p_cached + usage.output_tokens * p_out) / 1_000_000, 6)


# ---------------------------------------------------------------- JSON helpers
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_json_text(text: str) -> dict:
    cleaned = _FENCE.sub("", (text or "").strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise StructuredOutputError("response contained no JSON object") from None
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(f"invalid JSON: {exc.msg} at char {exc.pos}") from None
    if not isinstance(data, dict):
        raise StructuredOutputError("top-level JSON value must be an object")
    return data


def _error_snippet(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = (body.get("error") or {}) if isinstance(body, dict) else {}
        return (msg.get("message") if isinstance(msg, dict) else str(msg)) or resp.text[:200]
    except ValueError:
        return resp.text[:200]


def _raise_for_status(resp: httpx.Response, provider: str) -> None:
    if resp.status_code < 400:
        return
    detail = _error_snippet(resp)
    status = resp.status_code
    if status in (401, 403):
        raise LLMProviderError(f"{provider} rejected the API credentials (HTTP {status}).", status=status, kind="auth")
    if status == 404:
        raise LLMProviderError(f"{provider} model not found - check LLM_MODEL ({detail[:120]}).", status=status, kind="model")
    if status == 429:
        raise LLMProviderError(f"{provider} rate limit / quota reached.", retryable=True, status=status, kind="rate_limit")
    if status >= 500 or status == 408:
        raise LLMProviderError(f"{provider} is temporarily unavailable (HTTP {status}).", retryable=True, status=status)
    raise LLMProviderError(f"{provider} rejected the request (HTTP {status}): {detail[:200]}", status=status, kind="bad_request")


# ---------------------------------------------------------------- providers
class BaseProvider:
    name = ""

    def __init__(self, model: str, api_key: str, settings: Settings, client: httpx.AsyncClient):
        self.model, self.api_key, self.settings, self.client = model, api_key, settings, client

    async def complete(self, system: str, user: str, schema: dict) -> Completion:  # pragma: no cover
        raise NotImplementedError

    async def _post(self, url: str, headers: dict, body: dict) -> httpx.Response:
        started = time.perf_counter()
        try:
            resp = await self.client.post(url, headers=headers, json=body, timeout=self.settings.llm_timeout_seconds)
        except (httpx.TimeoutException,):
            raise LLMProviderError(f"{self.name} request timed out.", retryable=True, kind="timeout") from None
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"{self.name} connection error ({type(exc).__name__}).", retryable=True) from None
        resp.elapsed_ms = int((time.perf_counter() - started) * 1000)  # type: ignore[attr-defined]
        _raise_for_status(resp, self.name)
        return resp


class OpenAIProvider(BaseProvider):
    name = "openai"

    async def complete(self, system, user, schema) -> Completion:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": self.settings.llm_max_output_tokens,
        }
        if self.settings.openai_reasoning_effort:
            body["reasoning_effort"] = self.settings.openai_reasoning_effort
        resp = await self._post(OPENAI_URL, {"Authorization": f"Bearer {self.api_key}"}, body)
        data = resp.json()
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise LLMProviderError("OpenAI returned an unexpected response shape.", retryable=True) from None
        if choice.get("finish_reason") == "length":
            raise StructuredOutputError("model output was truncated (raise LLM_MAX_OUTPUT_TOKENS)")
        usage = data.get("usage") or {}
        cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        return Completion(text, max((usage.get("prompt_tokens") or 0) - cached, 0), cached,
                          usage.get("completion_tokens") or 0, resp.elapsed_ms)  # type: ignore[attr-defined]


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    async def complete(self, system, user, schema) -> Completion:
        body = {
            "model": self.model,
            "max_tokens": self.settings.llm_max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": TOOL_NAME, "description": "Submit the completed website analysis.", "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }
        headers = {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION}
        resp = await self._post(ANTHROPIC_URL, headers, body)
        data = resp.json()
        if data.get("stop_reason") == "max_tokens":
            raise StructuredOutputError("model output was truncated (raise LLM_MAX_OUTPUT_TOKENS)")
        block = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
        if block is None or not isinstance(block.get("input"), dict):
            raise StructuredOutputError("Claude did not return the requested tool call")
        usage = data.get("usage") or {}
        return Completion(json.dumps(block["input"]), usage.get("input_tokens") or 0,
                          usage.get("cache_read_input_tokens") or 0, usage.get("output_tokens") or 0,
                          resp.elapsed_ms)  # type: ignore[attr-defined]


class GeminiProvider(BaseProvider):
    name = "gemini"

    async def complete(self, system, user, schema) -> Completion:
        gen_config: dict = {"responseMimeType": "application/json", "maxOutputTokens": self.settings.llm_max_output_tokens}
        if self.settings.gemini_thinking_level:
            gen_config["thinkingConfig"] = {"thinkingLevel": self.settings.gemini_thinking_level}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen_config,
        }
        resp = await self._post(GEMINI_URL.format(model=self.model), {"x-goog-api-key": self.api_key}, body)
        data = resp.json()
        if (data.get("promptFeedback") or {}).get("blockReason"):
            raise StructuredOutputError(f"Gemini blocked the prompt ({data['promptFeedback']['blockReason']})")
        try:
            cand = data["candidates"][0]
            parts = cand.get("content", {}).get("parts", [])
        except (KeyError, IndexError, TypeError):
            raise StructuredOutputError("Gemini returned no candidates") from None
        if cand.get("finishReason") == "MAX_TOKENS":
            raise StructuredOutputError("model output was truncated (raise LLM_MAX_OUTPUT_TOKENS)")
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        u = data.get("usageMetadata") or {}
        cached = u.get("cachedContentTokenCount") or 0
        return Completion(text, max((u.get("promptTokenCount") or 0) - cached, 0), cached,
                          (u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0),
                          resp.elapsed_ms)  # type: ignore[attr-defined]


class GroqProvider(BaseProvider):
    name = "groq"

    async def complete(self, system: str, user: str, schema: dict) -> Completion:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": self.settings.llm_max_output_tokens,
        }
        resp = await self._post(GROQ_URL, {"Authorization": f"Bearer {self.api_key}"}, body)
        try:
            data = resp.json()
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, ValueError):
            raise LLMProviderError("Groq returned an unexpected response shape.", retryable=True) from None
        if choice.get("finish_reason") == "length":
            raise StructuredOutputError("model output was truncated (raise LLM_MAX_OUTPUT_TOKENS)")
        if not text.strip():
            raise StructuredOutputError("Groq returned empty response content.")
        usage = data.get("usage") or {}
        cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        return Completion(
            text,
            max((usage.get("prompt_tokens") or 0) - cached, 0),
            cached,
            usage.get("completion_tokens") or 0,
            resp.elapsed_ms,
        )  # type: ignore[attr-defined]


PROVIDERS: dict[str, type[BaseProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "groq": GroqProvider,
}


# ---------------------------------------------------------------- service
class LLMService:
    """Runs the structured-output call with retry -> repair -> fallback."""

    RETRY_DELAYS = (1.0, 3.0)

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.chain: list[BaseProvider] = []
        self.config_errors: list[str] = []
        pairs = [(settings.llm_provider, settings.llm_model)]
        if settings.llm_fallback_provider:
            pairs.append((settings.llm_fallback_provider, settings.llm_fallback_model))
        for provider, model in pairs:
            key = settings.api_key_for(provider)
            if not key:
                self.config_errors.append(f"{provider.upper()}_API_KEY is not set")
                continue
            self.chain.append(PROVIDERS[provider](settings.model_for(provider, model), key, settings, client))

    @property
    def primary_label(self) -> tuple[str, str]:
        return self.settings.llm_provider, self.settings.primary_model

    async def generate_structured(
        self, *, system: str, make_user_prompt: Callable[[Optional[str]], str], schema: dict,
        parse: Callable[[dict], T],
    ) -> tuple[T, LLMUsage]:
        if not self.chain:
            raise AppError(503, "llm_not_configured",
                           "No LLM is configured on the server (" + "; ".join(self.config_errors or ["no provider"]) + ").")
        failures: list[str] = []
        usage = LLMUsage()
        for index, provider in enumerate(self.chain):
            usage.provider, usage.model = provider.name, provider.model
            usage.fallback_used = index > 0
            try:
                result = await self._run(provider, system, make_user_prompt, schema, parse, usage)
                usage.cost_usd = estimate_cost(provider.name, provider.model, usage)
                return result, usage
            except (LLMProviderError, StructuredOutputError) as exc:
                failures.append(f"{provider.name}/{provider.model}: {exc}")
                log.warning("LLM attempt failed: %s", failures[-1])
        last = failures[-1] if failures else "unknown"
        if "credentials" in last:
            raise AppError(502, "llm_auth_error", "The server's LLM credentials were rejected by the provider.")
        if "rate limit" in last:
            raise AppError(503, "llm_rate_limited", "The LLM provider is rate limiting requests. Please retry shortly.",
                           headers={"Retry-After": "30"})
        if "validation" in last or "JSON" in last or "truncated" in last or "tool call" in last:
            raise AppError(502, "llm_invalid_response", "The LLM did not return a valid structured analysis. Please retry.")
        raise AppError(502, "llm_unavailable", "The LLM provider is currently unavailable. Please retry shortly.")

    async def _run(self, provider, system, make_user_prompt, schema, parse, usage: LLMUsage):
        repair_errors: Optional[str] = None
        for attempt in range(2):                      # attempt 0 = normal, attempt 1 = repair
            completion = await self._call_with_retry(provider, system, make_user_prompt(repair_errors), schema)
            usage.add(completion)
            try:
                return parse(parse_json_text(completion.text))
            except StructuredOutputError as exc:
                repair_errors = str(exc)
            except ValueError as exc:                 # pydantic.ValidationError subclasses ValueError
                repair_errors = _summarise_validation_error(exc)
            log.info("structured output invalid (attempt %d): %s", attempt + 1, repair_errors[:300])
            usage.repaired = True
        raise StructuredOutputError(f"validation failed after repair attempt: {repair_errors}")

    async def _call_with_retry(self, provider: BaseProvider, system: str, user: str, schema: dict) -> Completion:
        last: Exception | None = None
        for delay in (0.0, *self.RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await provider.complete(system, user, schema)
            except LLMProviderError as exc:
                last = exc
                if not exc.retryable:
                    raise
                log.warning("%s transient error, retrying: %s", provider.name, exc)
        assert last is not None
        raise last


def _summarise_validation_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        lines = []
        for e in errors()[:8]:
            loc = ".".join(str(p) for p in e.get("loc", ()))
            lines.append(f"- {loc}: {e.get('msg')}")
        return "validation errors:\n" + "\n".join(lines)
    return f"validation error: {exc}"
