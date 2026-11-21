"""Estimated cost of ONE website analysis per model, from published list prices.

    python -m scripts.cost_estimate [--input 6500] [--output 3500]

Token assumptions (ESTIMATES, replace with measured values from scripts/benchmark.py):
  input  ~6,500 = ~1,000 fixed prompt (system + JSON skeleton) + up to 20,000 chars (~5,000 tokens) of site text
  output ~3,500 = ~2,500 JSON (tests/fixtures/sample_llm_output.json is 8.7k chars ~ 2.5k tokens)
                  + ~1,000 reasoning/thinking tokens (billed as output)
"""
from __future__ import annotations

import argparse

from app.config import MODEL_PRICING


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=int, default=6500)
    ap.add_argument("--output", type=int, default=3500)
    args = ap.parse_args()

    rows = []
    for (provider, model), (p_in, p_out, _cached) in MODEL_PRICING.items():
        cost = (args.input * p_in + args.output * p_out) / 1_000_000
        rows.append((cost, provider, model, p_in, p_out))
    rows.sort()

    print(f"Assumed tokens per analysis: input={args.input:,}  output={args.output:,}\n")
    print("| Provider | Model | $/1M in | $/1M out | Est. cost / analysis | Analyses per $1 |")
    print("|---|---|---:|---:|---:|---:|")
    for cost, provider, model, p_in, p_out in rows:
        print(f"| {provider} | `{model}` | {p_in:.2f} | {p_out:.2f} | ${cost:.4f} | {1 / cost:,.0f} |")


if __name__ == "__main__":
    main()
