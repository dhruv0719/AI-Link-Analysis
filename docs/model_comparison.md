# LLM Model Comparison & Vendor Evaluation

This document compares the supported LLM providers for the **AI Website Link Analysis API**.

> [!IMPORTANT]
> **Methodology Distinction:**
> - **Measured Metrics:** Obtained directly on our active testbed using **Groq (`openai/gpt-oss-120b`)** with live network requests and token accounting.
> - **Published Specifications:** Sourced directly from vendor official pricing and documentation (OpenAI, Anthropic, Google Gemini) as of November 2026.

---

## 1. Comparison Matrix

| Provider | Model | Evaluation Type | Input Price / 1M | Output Price / 1M | Cache Read / 1M | Est. Cost / Analysis (~4.8k in / 2.8k out) | Measured / Typical Latency | Context Window | Structured Output Support |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| **Groq** | `openai/gpt-oss-120b` | **Measured (Live Testbed)** | $0.15 | $0.60 | $0.075 | **$0.00242** | **~8.9s – 10.5s** | 128k tokens | JSON Mode + Pydantic Schema |
| **OpenAI** | `gpt-6-luna` | Published Vendor Spec | $0.10 | $0.50 | $0.010 | ~$0.00188 | ~6.0s – 9.0s (spec) | 128k tokens | Native Structured Outputs (`json_schema`) |
| **Anthropic** | `claude-haiku-5-5` | Published Vendor Spec | $0.10 | $0.50 | $0.010 | ~$0.00188 | ~5.0s – 8.0s (spec) | 200k tokens | Tool Calling / Structured Output |
| **Google Gemini** | `gemini-3.5-flash-lite` | Published Vendor Spec | $0.30 | $2.50 | $0.030 | ~$0.00844 | ~4.0s – 7.0s (spec) | 1M tokens | Native Structured Output (`response_schema`) |

---

## 2. Detailed Provider Analysis

### A. Groq — `openai/gpt-oss-120b` (Measured on Live Testbed)
- **Status:** Primary active inference engine for our live evaluation.
- **Measured Performance:**
  - Average LLM inference time: **8.98s** (Plausible) to **9.12s** (Jay Jagle Innovations).
  - Token consumption: ~4,000–5,000 input tokens, ~2,400–2,900 completion tokens.
  - Per-request cost: **~$0.0020 – $0.0024 USD** (~1/4 of a cent per website analysis).
- **Strengths:**
  - High generation speed on LPU hardware.
  - Strong instruction-following for evidence quotations and multi-platform query generation.
  - Highly economical per-token pricing for open-weights models.
- **Production Caveats & Failure Modes (Measured):**
  - **Free-Tier Rate Limits:** Strict 6,000 TPM limit. Consecutive analyses without a cooldown period cause `HTTP 429 Too Many Requests`.
  - **JSON Mode Variation:** Occasionally wraps or formats the root element; requires the resilient JSON extractor/repair pipeline implemented in `app/services/llm_service.py`.

---

### B. OpenAI — `gpt-6-luna` (Published Specifications)
- **Status:** Default configured model for enterprise production deployments.
- **Published Pricing:** $0.10 / 1M input tokens, $0.50 / 1M output tokens, $0.01 / 1M cached input tokens.
- **Expected Cost per Run:** **~$0.00188 USD**.
- **Strengths:**
  - Guaranteed schema conformity via strict `json_schema` mode (`response_format: {"type": "json_schema", "strict": true}`).
  - High reasoning capability on nuanced competitive positioning and market differentiation.
  - Generous enterprise rate limits and predictable latency.
- **Production Caveats:**
  - Reasoning effort parameters (`OPENAI_REASONING_EFFORT=low`) must be tuned to prevent excessive generation latency.

---

### C. Anthropic — `claude-haiku-5-5` (Published Specifications)
- **Status:** Supported high-speed alternative provider.
- **Published Pricing:** $0.10 / 1M input tokens, $0.50 / 1M output tokens, $0.01 / 1M cached input tokens (for prompts <= 100k tokens).
- **Expected Cost per Run:** **~$0.00188 USD**.
- **Strengths:**
  - 200,000 token context window allows ingesting extensive multi-page website content without truncation.
  - Exceptional text comprehension and nuance in B2B audience segmentation.
  - Native prompt caching reduces recurring cost for shared system prompt instructions.
- **Production Caveats:**
  - Structured output requires tool-calling schema simulation.

---

### D. Google Gemini — `gemini-3.5-flash-lite` (Published Specifications)
- **Status:** Supported multi-modal and ultra-large context provider.
- **Published Pricing:** $0.30 / 1M input tokens, $2.50 / 1M output tokens, $0.03 / 1M cached input tokens.
- **Expected Cost per Run:** **~$0.00844 USD**.
- **Strengths:**
  - Massive 1,000,000 token context window.
  - Ultra-fast time-to-first-token (TTFT).
  - Strong adherence to Pydantic-based `response_schema`.
- **Production Caveats:**
  - Slightly higher output token pricing ($2.50/1M) compared to OpenAI/Anthropic ($0.50/1M) and Groq ($0.60/1M).

---

## 3. Fallback & Multi-Provider Architecture

The API implements an automatic multi-provider fallback chain configured in `app/config.py`:
```bash
LLM_PROVIDER=groq
LLM_MODEL=openai/gpt-oss-120b
LLM_FALLBACK_PROVIDER=openai
LLM_FALLBACK_MODEL=gpt-6-luna
```

If the primary provider encounters:
1. HTTP 429 (Rate limited)
2. HTTP 5xx (Upstream outage)
3. Schema invalidity after retry

The client transparently falls back to the secondary provider, recording the fallback execution in `response.meta.llm_provider_used`.
