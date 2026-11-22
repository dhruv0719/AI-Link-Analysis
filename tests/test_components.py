"""Unit tests: SSRF validators, safe fetcher, extractor, providers, retry logic, misc utils."""
from __future__ import annotations

import ipaddress
import json
from types import SimpleNamespace

import httpx
import pytest

import app.services.website_extractor as extractor_mod
from app.config import MODEL_PRICING, Settings
from app.errors import AppError
from app.prompts import OUTPUT_EXAMPLE
from app.schemas import LLMAnalysis
from app.services.llm_service import (AnthropicProvider, Completion, GeminiProvider, GroqProvider, LLMProviderError, LLMService,
                                      OpenAIProvider, PROVIDERS, estimate_cost, parse_json_text, StructuredOutputError)
from app.services.website_extractor import SafeFetcher, parse_page, pick_key_pages, seo_audit
from app.utils.cache import TTLCache
from app.utils.ratelimit import RateLimiter
from app.utils.validators import is_public_ip, normalize_url, resolve_public_ips
from tests.conftest import HOME_HTML, LLM_OUTPUT, FakeProvider, make_settings

pytestmark = pytest.mark.anyio


# ----------------------------------------------------------------------------- validators / SSRF
@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("93.184.216.34", True), ("2606:4700:4700::1111", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.1.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fe80::1", False), ("fc00::1", False), ("::ffff:10.0.0.1", False),
    ("64:ff9b::7f00:1", False), ("2002:7f00:1::", False),
])
def test_is_public_ip(ip, public):
    assert is_public_ip(ipaddress.ip_address(ip)) is public


def test_normalize_url_canonicalises():
    assert normalize_url("Example.COM") == "https://example.com/"
    assert normalize_url("http://example.com/a b".replace(" ", "%20")) == "http://example.com/a%20b"
    assert normalize_url("https://example.com/x?y=1#frag") == "https://example.com/x?y=1"
    assert normalize_url("https://bücher.example/") == "https://xn--bcher-kva.example/"


@pytest.mark.parametrize("bad", ["", "   ", "http://", "https://exa mple.com", "javascript:alert(1)", "https://example.com:22",
                                 "https://intranet", "https://printer.local", "http://x" * 400])
def test_normalize_url_rejects(bad):
    with pytest.raises(AppError) as exc:
        normalize_url(bad)
    assert exc.value.status_code in (400, 422)


def test_allow_private_flag_is_only_for_dev():
    assert normalize_url("http://127.0.0.1:8000/", allow_private=True) == "http://127.0.0.1:8000/"


async def test_resolver_blocks_numeric_host_tricks():
    for host in ("127.1", "2130706433"):              # resolved by the OS as 127.0.0.1
        with pytest.raises(AppError) as exc:
            await resolve_public_ips(host, 80)
        assert exc.value.code in ("blocked_url", "domain_not_found")
    assert await resolve_public_ips("8.8.8.8", 443) == ["8.8.8.8"]


# ----------------------------------------------------------------------------- SafeFetcher (httpx.MockTransport)
@pytest.fixture
def public_dns(monkeypatch):
    """example.com -> public IP, rebind.example -> private IP (simulates DNS pointing inside the network)."""
    table = {"example.com": "93.184.216.34", "www.example.com": "93.184.216.34", "rebind.example": "10.0.0.7"}

    async def fake_resolve(host, port, *, allow_private=False, timeout=5.0):
        assert timeout == 5.0, f"Expected default DNS timeout 5.0, got {timeout}"
        if host not in table:
            raise AppError(422, "domain_not_found", "nope")
        if not is_public_ip(ipaddress.ip_address(table[host])):
            raise AppError(400, "blocked_url", "private")
        return [table[host]]
    monkeypatch.setattr(extractor_mod, "resolve_public_ips", fake_resolve)


def fetcher_for(handler) -> SafeFetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SafeFetcher(client, make_settings())


async def test_fetch_follows_redirects_and_revalidates_each_hop(public_dns):
    hops = []

    def handler(req: httpx.Request):
        hops.append(str(req.url))
        if req.url.host == "example.com":
            return httpx.Response(301, headers={"location": "https://www.example.com/home"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=HOME_HTML)

    result = await fetcher_for(handler).fetch("http://example.com")
    assert result.url == "https://www.example.com/home" and len(hops) == 2


@pytest.mark.parametrize("location", ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8080/", "https://rebind.example/"])
async def test_redirect_to_internal_address_is_blocked(public_dns, location):
    hops = []

    def handler(req):
        hops.append(req.url.host)
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(AppError) as exc:
        await fetcher_for(handler).fetch("https://example.com")
    assert exc.value.code == "blocked_url"
    assert hops == ["example.com"]                      # the internal target was never requested


async def test_redirect_loop_is_capped(public_dns):
    with pytest.raises(AppError) as exc:
        await fetcher_for(lambda r: httpx.Response(302, headers={"location": "https://example.com/again"})).fetch("https://example.com")
    assert exc.value.code == "too_many_redirects"


@pytest.mark.parametrize("status,code", [(404, "page_not_found"), (403, "website_access_denied"), (429, "website_access_denied"), (500, "website_error")])
async def test_http_errors_are_mapped(public_dns, status, code):
    with pytest.raises(AppError) as exc:
        await fetcher_for(lambda r: httpx.Response(status)).fetch("https://example.com")
    assert exc.value.code == code


async def test_non_html_and_timeouts_and_connect_errors(public_dns):
    with pytest.raises(AppError) as exc:
        await fetcher_for(lambda r: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")).fetch("https://example.com")
    assert exc.value.code == "unsupported_content_type"

    def timeout(req): raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(AppError) as exc:
        await fetcher_for(timeout).fetch("https://example.com")
    assert exc.value.status_code == 504

    def refused(req): raise httpx.ConnectError("refused", request=req)
    with pytest.raises(AppError) as exc:
        await fetcher_for(refused).fetch("https://example.com")
    assert exc.value.code == "website_unreachable"


async def test_response_size_is_capped(public_dns):
    big = b"<html><body>" + b"<p>word word word word word</p>" * 200_000
    result = await fetcher_for(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=big)).fetch("https://example.com", max_bytes=50_000)
    assert result.truncated and len(result.body) <= 50_000


# ----------------------------------------------------------------------------- extraction
def test_parse_page_extracts_signal_and_drops_noise():
    p = parse_page(HOME_HTML, "https://acme.example/")
    text = " ".join(p.blocks)
    assert p.title.startswith("Acme Web Studio") and p.lang == "en" and p.canonical == "https://acme.example/"
    assert p.og["og:site_name"] == "Acme Web Studio" and p.jsonld[0]["@type"] == "Organization"
    assert p.nav_labels[:3] == ["Services", "About us", "Pricing"]
    assert "SHOULD NOT APPEAR" not in text and "cookies" not in text and "all rights reserved" not in text
    assert "Shopify and WooCommerce" in text                      # content inside <form> wrapper survived
    assert sum(b.lower().count("we build websites that win customers") for b in p.blocks) == 1   # de-duplicated
    assert p.social_links == ["https://www.facebook.com/acme"]


def test_key_page_discovery_prefers_services_about_pricing():
    p = parse_page(HOME_HTML, "https://acme.example/")
    assert set(pick_key_pages(p, 3)) == {"https://acme.example/services", "https://acme.example/about", "https://acme.example/pricing"}
    assert "https://acme.example/privacy" not in pick_key_pages(p, 10)
    assert pick_key_pages(p, 1) in (["https://acme.example/services"], ["https://acme.example/about"])


def test_seo_audit_flags_problems():
    bad = parse_page(b"<html><head><title>Hi</title></head><body><h1>A</h1><h1>B</h1><img src=x></body></html>", "https://x.example/")
    _, checks = seo_audit(bad)
    status = {c.check: c.status for c in checks}
    assert status["Title tag"] == "warn" and status["Meta description"] == "fail" and status["Single H1"] == "warn"
    assert status["Mobile viewport meta"] == "fail" and status["Image alt text"] == "fail"


# ----------------------------------------------------------------------------- prompt/schema sync
def test_prompt_example_matches_schema():
    import copy
    example = copy.deepcopy(OUTPUT_EXAMPLE)
    example["target_search_queries"] *= 5
    example["top_services"] *= 5
    example["competitor_analysis"]["comparison"][0]["role"] = "your_brand"
    LLMAnalysis.model_validate(example)           # raises if the skeleton drifts from the models


def test_platform_aliases_and_trimming():
    data = json.loads(json.dumps(LLM_OUTPUT))
    data["target_search_queries"][0]["platforms"] = ["Twitter", "google search", "FB", "TikTok"]
    data["target_search_queries"] = data["target_search_queries"] * 2  # 10 queries -> trimmed to 5
    parsed = LLMAnalysis.model_validate(data)
    assert parsed.target_search_queries[0].platforms == ["X", "Google", "Facebook"]
    assert len(parsed.target_search_queries) == 5


def test_parse_json_text_variants():
    assert parse_json_text('{"a": 1}') == {"a": 1}
    assert parse_json_text('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_text('Sure! Here you go: {"a": {"b": 2}} Hope it helps') == {"a": {"b": 2}}
    for bad in ("", "no json here", "[1, 2]", '{"a": '):
        with pytest.raises(StructuredOutputError):
            parse_json_text(bad)


# ----------------------------------------------------------------------------- provider request/response shapes
def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_openai_request_and_usage():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 7000, "completion_tokens": 3000,
                                                   "prompt_tokens_details": {"cached_tokens": 1000}}})
    p = OpenAIProvider("gpt-6-luna", "sk-secret", make_settings(), _client(handler))
    out = await p.complete("sys JSON", "user", {})
    assert seen["url"] == "https://api.openai.com/v1/chat/completions" and seen["auth"] == "Bearer sk-secret"
    assert seen["body"]["response_format"] == {"type": "json_object"} and seen["body"]["reasoning_effort"] == "low"
    assert seen["body"]["model"] == "gpt-6-luna" and "temperature" not in seen["body"]
    assert (out.input_tokens, out.cached_input_tokens, out.output_tokens) == (6000, 1000, 3000)


async def test_openai_reasoning_effort_can_be_disabled_and_truncation_detected():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{"}, "finish_reason": "length"}], "usage": {}})
    p = OpenAIProvider("gpt-4.1-mini", "k", make_settings(openai_reasoning_effort=""), _client(handler))
    with pytest.raises(StructuredOutputError):
        await p.complete("s", "u", {})
    assert "reasoning_effort" not in seen["body"]


async def test_anthropic_forced_tool_call():
    seen = {}

    def handler(req):
        seen.update(headers=req.headers, body=json.loads(req.content))
        return httpx.Response(200, json={"stop_reason": "tool_use", "content": [{"type": "text", "text": "ok"},
                                                                               {"type": "tool_use", "name": "submit_analysis", "input": {"a": 1}}],
                                         "usage": {"input_tokens": 6800, "output_tokens": 2900, "cache_read_input_tokens": 100}})
    p = AnthropicProvider("claude-haiku-5-5", "sk-ant", make_settings(), _client(handler))
    out = await p.complete("sys", "user", {"type": "object"})
    assert seen["headers"]["x-api-key"] == "sk-ant" and seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"]["tool_choice"] == {"type": "tool", "name": "submit_analysis"}
    assert json.loads(out.text) == {"a": 1} and (out.input_tokens, out.cached_input_tokens, out.output_tokens) == (6800, 100, 2900)


async def test_gemini_request_counts_thinking_tokens_as_output():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), key=req.headers["x-goog-api-key"], body=json.loads(req.content))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}, {"text": '{"a": 2}'}]}, "finishReason": "STOP"}],
                                         "usageMetadata": {"promptTokenCount": 7000, "candidatesTokenCount": 2800, "thoughtsTokenCount": 700}})
    p = GeminiProvider("gemini-3.5-flash-lite", "g-key", make_settings(gemini_thinking_level="low"), _client(handler))
    out = await p.complete("sys", "user", {})
    assert seen["url"].endswith("/models/gemini-3.5-flash-lite:generateContent") and seen["key"] == "g-key"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert out.text == '{"a": 2}' and out.output_tokens == 3500 and out.input_tokens == 7000


async def test_groq_request_and_usage():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 6000,
                "completion_tokens": 2500,
                "prompt_tokens_details": {"cached_tokens": 1000},
            },
        })

    p = GroqProvider("openai/gpt-oss-120b", "gsk-secret", make_settings(), _client(handler))
    out = await p.complete("sys JSON", "user", {})
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer gsk-secret"
    assert seen["body"]["model"] == "openai/gpt-oss-120b"
    assert seen["body"]["messages"] == [{"role": "system", "content": "sys JSON"}, {"role": "user", "content": "user"}]
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["max_completion_tokens"] == 8000
    assert out.text == '{"ok": true}'
    assert (out.input_tokens, out.cached_input_tokens, out.output_tokens) == (5000, 1000, 2500)


async def test_groq_truncation_detected():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "{"}, "finish_reason": "length"}], "usage": {}})
    p = GroqProvider("openai/gpt-oss-120b", "k", make_settings(), _client(handler))
    with pytest.raises(StructuredOutputError, match="truncated"):
        await p.complete("s", "u", {})


async def test_groq_empty_and_malformed_response():
    # Empty content
    p_empty = GroqProvider("openai/gpt-oss-120b", "k", make_settings(),
                           _client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "   "}, "finish_reason": "stop"}], "usage": {}})))
    with pytest.raises(StructuredOutputError, match="empty response content"):
        await p_empty.complete("s", "u", {})

    # Missing choices / malformed shape
    for bad_json in ({}, {"choices": []}, {"choices": [{}]}):
        p_bad = GroqProvider("openai/gpt-oss-120b", "k", make_settings(), _client(lambda r, b=bad_json: httpx.Response(200, json=b)))
        with pytest.raises(LLMProviderError) as exc:
            await p_bad.complete("s", "u", {})
        assert exc.value.retryable is True


@pytest.mark.parametrize("status,retryable,kind", [
    (401, False, "auth"),
    (404, False, "model"),
    (400, False, "bad_request"),
    (429, True, "rate_limit"),
    (500, True, "error"),
    (503, True, "error"),
])
async def test_groq_http_status_classification(status, retryable, kind):
    p = GroqProvider("openai/gpt-oss-120b", "k", make_settings(), _client(lambda r: httpx.Response(status, json={"error": {"message": "groq boom"}})))
    with pytest.raises(LLMProviderError) as exc:
        await p.complete("s", "u", {})
    assert exc.value.retryable is retryable and exc.value.kind == kind


async def test_groq_timeout_handling():
    def timeout_handler(req):
        raise httpx.ReadTimeout("Groq read timed out", request=req)
    p = GroqProvider("openai/gpt-oss-120b", "k", make_settings(), _client(timeout_handler))
    with pytest.raises(LLMProviderError) as exc:
        await p.complete("s", "u", {})
    assert exc.value.retryable is True and exc.value.kind == "timeout"


async def test_groq_api_key_protection():
    secret = "gsk-SUPER-SECRET-GROQ-123"
    settings = make_settings(groq_api_key=secret)
    p = GroqProvider("openai/gpt-oss-120b", secret, settings, _client(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}})))
    with pytest.raises(LLMProviderError) as exc:
        await p.complete("s", "u", {})
    assert secret not in str(exc.value)
    assert secret not in repr(settings)


def test_groq_registration_and_settings():
    assert PROVIDERS["groq"] is GroqProvider
    settings = make_settings(llm_provider="groq", groq_api_key="gsk-configured-key", llm_model=None)
    assert settings.api_key_for("groq") == "gsk-configured-key"
    assert settings.model_for("groq") == "openai/gpt-oss-120b"
    assert settings.primary_model == "openai/gpt-oss-120b"
    service = LLMService(settings, _client(lambda r: httpx.Response(200)))
    assert len(service.chain) == 1
    assert service.chain[0].name == "groq"
    assert service.chain[0].model == "openai/gpt-oss-120b"
    assert service.chain[0].api_key == "gsk-configured-key"


@pytest.mark.parametrize("status,retryable,kind", [(401, False, "auth"), (404, False, "model"), (400, False, "bad_request"),
                                                   (429, True, "rate_limit"), (503, True, "error")])
async def test_http_status_classification(status, retryable, kind):
    p = OpenAIProvider("m", "k", make_settings(), _client(lambda r: httpx.Response(status, json={"error": {"message": "boom"}})))
    with pytest.raises(LLMProviderError) as exc:
        await p.complete("s", "u", {})
    assert exc.value.retryable is retryable and exc.value.kind == kind


async def test_api_key_never_appears_in_errors_or_settings_repr():
    secret = "sk-SUPER-SECRET-123"
    p = OpenAIProvider("m", secret, make_settings(openai_api_key=secret), _client(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}})))
    with pytest.raises(LLMProviderError) as exc:
        await p.complete("s", "u", {})
    assert secret not in str(exc.value) and secret not in repr(make_settings(openai_api_key=secret))


# ----------------------------------------------------------------------------- retry / service-level
async def test_transient_errors_are_retried_then_succeed():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(LLM_OUTPUT)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    settings = make_settings()
    service = LLMService(settings, _client(handler))
    service.RETRY_DELAYS = (0.0, 0.0)
    parsed, usage = await service.generate_structured(system="s", make_user_prompt=lambda e: "u", schema={}, parse=LLMAnalysis.model_validate)
    assert calls["n"] == 3 and parsed.brand.name == "Acme Web Studio" and usage.calls == 1


async def test_non_retryable_error_is_not_retried():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(401, json={})
    service = LLMService(make_settings(), _client(handler))
    with pytest.raises(AppError) as exc:
        await service.generate_structured(system="s", make_user_prompt=lambda e: "u", schema={}, parse=LLMAnalysis.model_validate)
    assert calls["n"] == 1 and exc.value.code == "llm_auth_error"


# ----------------------------------------------------------------------------- utils
def test_cost_estimate_matches_published_rates():
    c = Completion("x", input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost("openai", "gpt-6-luna", c) == pytest.approx(0.60)
    assert estimate_cost("gemini", "gemini-3.5-flash-lite", c) == pytest.approx(2.80)
    assert estimate_cost("anthropic", "claude-sonnet-5-5", c) == pytest.approx(12.0)
    assert estimate_cost("groq", "openai/gpt-oss-120b", c) == pytest.approx(0.75)
    assert estimate_cost("openai", "unknown-model", c) is None
    assert ("anthropic", "claude-haiku-5-5") in MODEL_PRICING
    assert ("groq", "openai/gpt-oss-120b") in MODEL_PRICING


def test_ttl_cache_expiry_and_lru(monkeypatch):
    cache = TTLCache(ttl_seconds=10, max_items=2)
    now = {"t": 1000.0}
    monkeypatch.setattr("app.utils.cache.time.monotonic", lambda: now["t"])
    cache.set("a", 1); cache.set("b", 2); cache.set("c", 3)
    assert cache.get("a") is None and cache.get("b") == 2          # LRU eviction
    now["t"] += 11
    assert cache.get("b") is None                                  # TTL expiry


def test_rate_limiter_blocks_after_limit():
    limiter = RateLimiter(per_minute=2)
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"))
    limiter.check(req); limiter.check(req)
    with pytest.raises(AppError) as exc:
        limiter.check(req)
    assert exc.value.status_code == 429 and "Retry-After" in exc.value.headers
    limiter.check(SimpleNamespace(headers={}, client=SimpleNamespace(host="5.6.7.8")))   # other client unaffected


def test_forwarded_header_only_trusted_when_enabled():
    req = SimpleNamespace(headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"}, client=SimpleNamespace(host="10.0.0.1"))
    assert RateLimiter(5, trust_proxy_headers=True)._client_id(req) == "9.9.9.9"
    assert RateLimiter(5, trust_proxy_headers=False)._client_id(req) == "10.0.0.1"
