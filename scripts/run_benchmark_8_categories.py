"""Benchmark runner across the 8 commercial categories.

Evaluates AI Website Link Analysis API across:
1. SaaS
2. E-commerce
3. Digital Marketing Agency
4. Healthcare / Telehealth
5. EdTech / Learning
6. Local Services / Trades
7. Developer Platform / Open Source
8. Industrial / B2B Manufacturing

Usage:
    # Run deterministic offline evaluation using benchmark fixtures:
    python -m scripts.run_benchmark_8_categories --offline

    # Run live evaluation with configured LLM (Groq / OpenAI / etc.):
    python -m scripts.run_benchmark_8_categories --live
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

from app.config import get_settings
from app.errors import AppError
from app.main import build_service
from app.schemas import LinkAnalysisResponse
from app.utils.cache import TTLCache
from scripts.evaluator import EvaluationResult, evaluate_analysis, format_evaluation_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("benchmark.8categories")

BENCHMARK_FIXTURES_PATH = Path("benchmarks/categories_fixtures.json")
DEFAULT_OUT_JSON = Path("benchmarks/categories_eval_results.json")
DEFAULT_REPORT_MD = Path("docs/benchmark_results.md")

TARGET_URLS = {
    "SaaS": "https://plausible.io",
    "E-commerce": "https://www.allbirds.com",
    "Digital Marketing": "https://www.singlegrain.com",
    "Healthcare": "https://ro.co",
    "EdTech": "https://www.coursera.org",
    "Local Services": "https://www.rotorooter.com",
    "Developer Platform": "https://github.com",
    "Industrial B2B": "https://www.fastenal.com",
}


def run_offline_evaluation(fixtures_path: Path) -> List[EvaluationResult]:
    """Run evaluation against pre-recorded or fixture responses for all 8 categories."""
    if not fixtures_path.exists():
        raise FileNotFoundError(f"Fixture file not found: {fixtures_path}")
    
    data = json.loads(fixtures_path.read_text(encoding="utf-8"))
    results: List[EvaluationResult] = []
    
    for category, payload in data.items():
        log.info(f"Evaluating {category} (URL: {payload.get('website')})")
        res = evaluate_analysis(payload, category=category)
        results.append(res)
    return results


async def run_live_evaluation(categories: List[str] | None = None) -> List[EvaluationResult]:
    """Run live pipeline analysis and evaluation using the configured LLM provider."""
    settings = get_settings()
    results: List[EvaluationResult] = []
    
    active_categories = {k: v for k, v in TARGET_URLS.items() if not categories or k in categories}
    
    async with httpx.AsyncClient() as client:
        service = build_service(settings, client)
        service.cache = TTLCache(0, 0)  # bypass cache for fresh evaluation
        
        for category, url in active_categories.items():
            log.info(f"Live testing {category}: {url}")
            t0 = time.perf_counter()
            try:
                # Extract site first to have raw pages for quote verification
                extracted = await service.extractor.extract_site(url)
                resp = await service.analyze(url)
                data = resp.model_dump()
                eval_res = evaluate_analysis(data, category=category, pages=extracted.pages)
                eval_res.details["elapsed_wall_s"] = round(time.perf_counter() - t0, 2)
                results.append(eval_res)
                log.info(f"Completed {category} with score {eval_res.overall_score:.0%}")
            except AppError as e:
                log.error(f"AppError for {category} ({url}): {e.code} - {e.message}")
                eval_res = EvaluationResult(category=category, url=url)
                eval_res.scores = {k: 0.0 for k in [
                    "website_understanding", "grounding_accuracy", "competitor_intelligence",
                    "search_query_quality", "seo_quality", "services_and_audience"
                ]}
                eval_res.details["error"] = e.code
                eval_res.requires_human_review.append(f"Analysis failed with {e.code}: {e.message}")
                results.append(eval_res)
            except Exception as e:
                log.exception(f"Unexpected error for {category} ({url})")
                eval_res = EvaluationResult(category=category, url=url)
                eval_res.scores = {k: 0.0 for k in [
                    "website_understanding", "grounding_accuracy", "competitor_intelligence",
                    "search_query_quality", "seo_quality", "services_and_audience"
                ]}
                eval_res.details["error"] = str(e)
                eval_res.requires_human_review.append(f"Unexpected exception: {str(e)}")
                results.append(eval_res)
            
            # Short cooldown between calls to prevent rate limits
            await asyncio.sleep(2.0)
            
    return results


def generate_markdown_report(results: List[EvaluationResult], mode: str) -> str:
    """Generate a full markdown audit report for docs/benchmark_results.md."""
    lines = [
        "# Phase 3.4 Benchmark Evaluation Results",
        "",
        f"**Evaluation Mode:** `{mode.upper()}`  ",
        f"**Categories Evaluated:** {len(results)}  ",
        f"**Timestamp:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  ",
        "",
        "## 1. Executive Summary Table",
        "",
        format_evaluation_summary(results),
        "",
        "## 2. Detailed Per-Category Breakdown",
        "",
    ]
    
    avg_score = sum(r.overall_score for r in results) / len(results) if results else 0.0
    lines.insert(6, f"**Average Overall Quality Score:** `{avg_score:.1%}`\n")
    
    for r in results:
        lines.append(f"### {r.category} ({r.url})")
        lines.append(f"- **Overall Score:** `{r.overall_score:.0%}`")
        lines.append("- **Dimension Scores:**")
        for k, v in r.scores.items():
            lines.append(f"  - `{k}`: {v:.0%}")
        lines.append("- **Verification Checks:**")
        for k, v in r.checks.items():
            status = "PASS" if v else "FAIL"
            lines.append(f"  - `{k}`: **{status}**")
        if r.details:
            lines.append("- **Key Metrics:**")
            for k, v in r.details.items():
                lines.append(f"  - `{k}`: {v}")
        if r.requires_human_review:
            lines.append("- **Human Review Flags:**")
            for h in r.requires_human_review:
                lines.append(f"  - {h}")
        lines.append("")

    lines.extend([
        "## 3. Evaluation Methodology",
        "",
        "The evaluation framework (`scripts/evaluator.py`) evaluates analysis quality across 6 key dimensions:",
        "1. **Website Understanding**: Brand identity, category categorization, and summary adequacy.",
        "2. **Grounding & Provenance**: Evidence quotation matching, exact normalized matching, provenance source attribution (`website` vs `inferred`).",
        "3. **Competitor Intelligence**: Reachability vs. relevance status separation, comparison table alignment.",
        "4. **Search Query Quality**: 5 primary target queries and full coverage across all 5 platforms (Google, Facebook, Instagram, LinkedIn, X).",
        "5. **SEO Improvements**: Title boundary compliance (<=65 chars), meta description compliance (70-165 chars), on-page deterministic checks.",
        "6. **Services & Audience**: Cardinality compliance (0-5 services), audience customer needs, and actionable marketing segments.",
        "",
        "## 4. How to Reproduce Locally",
        "",
        "```bash",
        "# Run deterministic offline evaluation (fixtures):",
        "python -m scripts.run_benchmark_8_categories --offline",
        "",
        "# Run live evaluation with configured LLM (requires API key in .env):",
        "python -m scripts.run_benchmark_8_categories --live",
        "```",
    ])
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Run 8-Category Benchmark Evaluation")
    parser.add_argument("--offline", action="store_true", help="Run offline evaluation using benchmark fixtures")
    parser.add_argument("--live", action="store_true", help="Run live pipeline evaluation against real websites")
    parser.add_argument("--fixtures", type=Path, default=BENCHMARK_FIXTURES_PATH, help="Path to fixtures JSON")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_JSON, help="Path to output JSON")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_MD, help="Path to output markdown report")
    args = parser.parse_args()

    if not args.offline and not args.live:
        # Default to offline if not specified
        args.offline = True

    if args.offline:
        log.info("Running OFFLINE benchmark evaluation...")
        results = run_offline_evaluation(args.fixtures)
        mode = "offline"
    else:
        log.info("Running LIVE benchmark evaluation...")
        results = asyncio.run(run_live_evaluation())
        mode = "live"

    # Save output JSON
    args.out.parent.mkdir(parents=True, exist_ok=True)
    serialized = [r.to_dict() for r in results]
    args.out.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
    log.info(f"Saved evaluation results to {args.out}")

    # Generate and save Markdown Report
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report_md = generate_markdown_report(results, mode)
    args.report.write_text(report_md, encoding="utf-8")
    log.info(f"Saved benchmark report to {args.report}")

    print("\n" + format_evaluation_summary(results) + "\n")
    avg = sum(r.overall_score for r in results) / len(results) if results else 0
    print(f"Average Benchmark Score: {avg:.1%}")


if __name__ == "__main__":
    main()
