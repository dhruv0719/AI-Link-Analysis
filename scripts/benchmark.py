"""Measure latency, cost, JSON reliability and grounding for several LLMs on the real pipeline.

    # needs the matching API keys in .env / environment
    python -m scripts.benchmark --models openai:gpt-6-luna anthropic:claude-haiku-5-5 gemini:gemini-3.5-flash-lite \
        --urls scripts/sample_urls.txt --runs 1 --out benchmarks/results.json

Everything printed here is MEASURED from real calls. Accuracy is approximated with objective proxies
(schema-valid on first try, % of 'website' services whose name appears in the page text, % of suggested
competitors whose live site mentions their name). Add the manual 1-5 rubric from the README on top.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from pathlib import Path

import httpx

from app.config import get_settings
from app.errors import AppError
from app.main import build_service
from app.utils.cache import TTLCache


def load_urls(path: str) -> list[str]:
    return [l.strip() for l in Path(path).read_text().splitlines() if l.strip() and not l.startswith("#")]


def grounded_ratio(services: list[dict], corpus: str) -> float | None:
    """Share of 'website'-sourced services whose distinctive words appear in the scraped text."""
    claimed = [s for s in services if s.get("source") == "website"]
    if not claimed:
        return None
    ok = 0
    for s in claimed:
        words = [w for w in re.findall(r"[a-z0-9]{4,}", s["name"].lower())]
        ok += bool(words) and sum(w in corpus for w in words) >= max(1, len(words) // 2)
    return ok / len(claimed)


def verified_ratio(competitors: list[dict]) -> float | None:
    if not competitors:
        return None
    return sum(c["verification"] == "verified_live_site" for c in competitors) / len(competitors)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def summarise(results: list[dict]) -> str:
    by_model: dict[str, list[dict]] = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)
    lines = ["| Model | Runs | Success | Repaired | LLM p50 / p95 (s) | Avg in / out tokens | Avg cost ($) | Services grounded | Competitors verified |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for model, rows in by_model.items():
        ok = [r for r in rows if r["success"]]

        def avg(key):
            vals = [r[key] for r in ok if r.get(key) is not None]
            return statistics.mean(vals) if vals else None

        def fmt(v, spec): return "n/a" if v is None else format(v, spec)
        llm_s = [r["llm_ms"] / 1000 for r in ok]
        lines.append(
            f"| `{model}` | {len(rows)} | {len(ok)}/{len(rows)} | {sum(r.get('repaired', False) for r in ok)} | "
            f"{fmt(pct(llm_s, .5) if llm_s else None, '.1f')} / {fmt(pct(llm_s, .95) if llm_s else None, '.1f')} | "
            f"{fmt(avg('input_tokens'), ',.0f')} / {fmt(avg('output_tokens'), ',.0f')} | {fmt(avg('cost'), '.4f')} | "
            f"{fmt(avg('grounded'), '.0%')} | {fmt(avg('verified'), '.0%')} |")
    return "\n".join(lines)


async def run(args) -> list[dict]:
    base = get_settings()
    urls = load_urls(args.urls)
    results: list[dict] = []
    async with httpx.AsyncClient() as client:
        corpus_cache: dict[str, str] = {}
        for spec in args.models:
            provider, model = spec.split(":", 1)
            settings = base.model_copy(update={"llm_provider": provider, "llm_model": model, "llm_fallback_provider": None,
                                               "verify_competitors": True})
            service = build_service(settings, client)
            service.cache = TTLCache(0, 0)                       # never serve cached results while benchmarking
            for url in urls:
                for run_no in range(args.runs):
                    row = {"model": spec, "url": url, "run": run_no, "success": False}
                    t0 = time.perf_counter()
                    try:
                        resp = await service.analyze(url)
                        data = resp.model_dump()
                        if url not in corpus_cache:
                            corpus_cache[url] = (await service.extractor.extract_site(url)).plain_text
                        m = data["meta"]
                        row.update(success=True, llm_ms=m["timings_ms"]["llm"], total_ms=m["timings_ms"]["total"],
                                   input_tokens=m["input_tokens"] + m["cached_input_tokens"], output_tokens=m["output_tokens"],
                                   cost=m["estimated_cost_usd"], repaired=m["repaired_output"],
                                   grounded=grounded_ratio(data["analysis"]["top_services"], corpus_cache[url]),
                                   verified=verified_ratio(data["analysis"]["competitor_analysis"]["competitors"]))
                    except AppError as exc:
                        row.update(error=exc.code, total_ms=int((time.perf_counter() - t0) * 1000))
                    results.append(row)
                    print(f"{spec:40} {url:38} {'ok ' if row['success'] else 'ERR'} "
                          f"{row.get('llm_ms', '-')}ms {row.get('error', '')}", flush=True)
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True, help="provider:model, e.g. openai:gpt-6-luna")
    ap.add_argument("--urls", default="scripts/sample_urls.txt")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", default="benchmarks/results.json")
    args = ap.parse_args()
    results = asyncio.run(run(args))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print("\n" + summarise(results))


if __name__ == "__main__":
    main()
