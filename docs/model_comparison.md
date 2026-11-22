# LLM Model Comparison & Vendor Evaluation

This document compares the supported LLM providers for the **AI Website Link Analysis API**.

> [!IMPORTANT]
> **CRITICAL METHODOLOGY DISTINCTION:**
> - **Measured Metrics:** Obtained directly on our active testbed using **Groq (`openai/gpt-oss-120b`)** with real network requests, actual token usage, and live timing logs. **Groq is the only currently tested provider on our live testbed.**
> - **Published Specifications:** Sourced directly from vendor official documentation and published pricing pages (OpenAI, Anthropic, Google Gemini) as of November 2026. These models have not been evaluated in live end-to-end runs in this testbed.

---

## 1. Comparison Matrix

| Provider | Model ID | Evaluation Status | Input Price / 1M | Output Price / 1M | Cache Read / 1M | Est. Cost / Analysis (~4.5k in / 2.5k out) | Latency | Context Window | Structured Output Mechanism |
|---|---|---|---:|---:|---:|---:|---|---|---|
| **Groq** | `openai/gpt-oss-120b` | **Measured (Live Testbed)** | $0.15 | $0.60 | $0.075 | **$0.0020 – $0.0024** | **8.9s – 10.5s (Measured)** | 128k tokens | JSON Mode + Pydantic Schema |
| **OpenAI** | `gpt-6-luna` | Published Specification | $0.10 | $0.50 | $0.010 | ~$0.00170 (Published Spec) | 6.0s – 9.0s (Vendor Spec) | 128k tokens | Native Strict `json_schema` |
| **Anthropic** | `claude-haiku-5-5` | Published Specification | $0.10 | $0.50 | $0.010 | ~$0.00170 (Published Spec) | 5.0s – 8.0s (Vendor Spec) | 200k tokens | Forced Tool Calling Schema |
| **Google Gemini** | `gemini-3.5-flash-lite` | Published Specification | $0.30 | $2.50 | $0.030 | ~$0.00760 (Published Spec) | 4.0s – 7.0s (Vendor Spec) | 1M tokens | Native `response_schema` |

---

## 2. Detailed Provider Analysis

### A. Groq — `openai/gpt-oss-120b` (MEASURED ON LIVE TESTBED)
- **Deployment Status:** Active primary inference engine in the benchmark environment.
- **Measured Performance (Live Invocations):**
  - Average LLM inference time: **8.98s** (Plausible) to **9.12s** (Jay Jagle Innovations).
  - Typical token consumption: ~4,000–4,800 prompt tokens, ~2,400–2,800 completion tokens.
  - Per-analysis cost: **~$0.0020 – $0.0024 USD** (~1/4 of a cent per website analysis).
- **Observed Strengths:**
  - Fast execution on Groq LPU hardware.
  - Strong instruction-following for exact verbatim quotes and 5-platform query generation.
  - Open-weights cost efficiency ($0.15/$0.60 per million tokens).
- **Operational Failure Modes & Limits Discovered in Live Testing:**
  1. **HTTP 413 (Payload Too Large / Requested Tokens Exceed TPM):**
     - When prompt text was unconstrained (e.g. 20,000-character extraction on Coursera or GitHub), total requested tokens reached 8,115. Because Groq's tier imposes an 8,000 TPM ceiling, Groq rejected the request with `HTTP 413: Limit 8000, Requested 8115`.
     - *Fix Implemented:* Bounded website content budget to 10,000 characters and set `max_completion_tokens` to 4,000, keeping total request size ~4,500–5,000 tokens.
  2. **HTTP 429 (Rate Limit / Quota Exceeded):**
     - Running consecutive requests without a delay immediately exhausts the 8,000 TPM quota window.
     - *Fix Implemented:* Enforced sequential execution with a 30-second pacing delay between benchmark runs.
  3. **JSON Object Formatting:**
     - Groq JSON Mode occasionally prefixes formatting whitespace; handled by the resilient `parse_json_text` parser in `app/services/llm_service.py`.

---

### B. OpenAI — `gpt-6-luna` (PUBLISHED SPECIFICATION — NOT LIVE TESTED)
- **Deployment Status:** Supported via HTTPX adapter; not tested on live testbed.
- **Published Pricing:** $0.10 / 1M input tokens, $0.50 / 1M output tokens, $0.01 / 1M cached input tokens.
- **Theoretical Cost per Analysis:** ~$0.00170 USD.
- **Documented Vendor Capabilities:**
  - Strict JSON schema enforcement via `response_format: {"type": "json_schema", "strict": true}`.
  - Configurable reasoning effort (`OPENAI_REASONING_EFFORT=low`).
  - Standard enterprise rate limits significantly higher than Groq free tier.
- **Known Trade-offs:**
  - Proprietary cloud API dependency.

---

### C. Anthropic — `claude-haiku-5-5` (PUBLISHED SPECIFICATION — NOT LIVE TESTED)
- **Deployment Status:** Supported via HTTPX adapter; not tested on live testbed.
- **Published Pricing:** $0.10 / 1M input tokens, $0.50 / 1M output tokens (for prompts <= 100k tokens), $0.01 / 1M cache read.
- **Theoretical Cost per Analysis:** ~$0.00170 USD.
- **Documented Vendor Capabilities:**
  - 200,000 token context window.
  - Tool calling with JSON schema validation.
  - Fast time-to-first-token.
- **Known Trade-offs:**
  - Price tier jumps 5x for prompts > 100k tokens (though our prompts remain ~5k tokens).

---

### D. Google Gemini — `gemini-3.5-flash-lite` (PUBLISHED SPECIFICATION — NOT LIVE TESTED)
- **Deployment Status:** Supported via HTTPX adapter; not tested on live testbed.
- **Published Pricing:** $0.30 / 1M input tokens, $2.50 / 1M output tokens, $0.03 / 1M cached input tokens. Free tier available with rate limits.
- **Theoretical Cost per Analysis:** ~$0.00760 USD.
- **Documented Vendor Capabilities:**
  - 1,000,000 token context window.
  - Native structured output via `responseMimeType: "application/json"`.
- **Known Trade-offs:**
  - Higher output pricing ($2.50/1M) compared to OpenAI/Anthropic/Groq.

---

## 3. Provider Fallback Architecture

The application configures multi-provider fallback in `app/config.py`:
```bash
LLM_PROVIDER=groq
LLM_MODEL=openai/gpt-oss-120b
LLM_FALLBACK_PROVIDER=openai
LLM_FALLBACK_MODEL=gpt-6-luna
```

If the primary provider encounters HTTP 429 (rate limits), HTTP 5xx (upstream outage), or invalid JSON after repair, the system transparently escalates to the fallback provider.
