"""Benchmark runner across 8 commercial categories with operational vs quality separation.

Features:
- Separate operational reliability metrics (attempts, 403 blocks, 429 rate limits, 413 payload limits, timeouts)
- Quality scores computed strictly on successfully analyzed websites (N/A if no successful run)
- Offline fixture consistency validation separated from real-world model accuracy
- Single-category (--category), multi-category (--categories), and sequential execution
- Configurable inter-request delay (--delay, default 30s) to respect provider TPM windows
- State persistence after each category and resume support (--resume)
- Accessible fallback targets for sites blocked by bot protection (with failure record preserved)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings
from app.errors import AppError
from app.main import build_service
from app.schemas import LinkAnalysisResponse
from app.utils.cache import TTLCache
from scripts.evaluator import (
    EvaluationResult,
    OperationalMetrics,
    evaluate_analysis,
    format_fixture_consistency_summary,
    format_operational_table,
    format_quality_summary,
    load_ground_truth,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("benchmark.runner")

BENCHMARK_FIXTURES_PATH = Path("benchmarks/categories_fixtures.json")
DEFAULT_OUT_JSON = Path("benchmarks/categories_eval_results.json")
DEFAULT_STATE_JSON = Path("benchmarks/benchmark_state.json")
DEFAULT_REPORT_MD = Path("docs/benchmark_results.md")

ALL_CATEGORIES = [
    "SaaS",
    "E-commerce",
    "Digital Marketing",
    "Healthcare",
    "EdTech",
    "Local Services",
    "Developer Platform",
    "Industrial B2B",
]

TARGET_CONFIG: Dict[str, Dict[str, Any]] = {
    "SaaS": {
        "primary_url": "https://plausible.io",
        "fallback_url": None,
    },
    "E-commerce": {
        "primary_url": "https://www.allbirds.com",
        "fallback_url": None,
    },
    "Digital Marketing": {
        "primary_url": "https://www.singlegrain.com",
        "fallback_url": "https://jayjagleinnovations.com",
    },
    "Healthcare": {
        "primary_url": "https://ro.co",  # Known 403 bot protection
        "fallback_url": "https://www.teladoc.com",
    },
    "EdTech": {
        "primary_url": "https://www.coursera.org",
        "fallback_url": "https://www.khanacademy.org",
    },
    "Local Services": {
        "primary_url": "https://www.rotorooter.com",
        "fallback_url": None,
    },
    "Developer Platform": {
        "primary_url": "https://github.com",
        "fallback_url": None,
    },
    "Industrial B2B": {
        "primary_url": "https://www.fastenal.com",  # Known 403 bot protection
        "fallback_url": "https://www.emachineshop.com",
    },
}


def run_offline_evaluation(fixtures_path: Path) -> List[EvaluationResult]:
    """Run offline consistency validation against prepared fixture responses."""
    if not fixtures_path.exists():
        raise FileNotFoundError(f"Fixture file not found: {fixtures_path}")

    data = json.loads(fixtures_path.read_text(encoding="utf-8"))
    results: List[EvaluationResult] = []

    for category, payload in data.items():
        log.info(f"Validating fixture for {category} (URL: {payload.get('website')})")
        res = evaluate_analysis(payload, category=category, is_fixture=True)
        results.append(res)
    return results


async def execute_target_analysis(
    service,
    category: str,
    url: str,
    op: OperationalMetrics,
) -> Optional[EvaluationResult]:
    """Execute analysis for a single URL and record operational outcomes."""
    log.info(f"Attempting {category}: {url}")
    t0 = time.perf_counter()
    try:
        extracted = await service.extractor.extract_site(url)
        resp = await service.analyze(url)
        data = resp.model_dump()
        eval_res = evaluate_analysis(data, category=category, pages=extracted.pages, is_fixture=False)
        eval_res.details["elapsed_wall_s"] = round(time.perf_counter() - t0, 2)
        op.record_success(category, url)
        log.info(f"Successfully analyzed {category} ({url}) with Quality Score: {eval_res.quality_score:.0%}")
        return eval_res
    except AppError as e:
        log.warning(f"Operational failure for {category} ({url}): {e.code} - {e.message}")
        op.record_failure(category, url, e.code, e.message)
        return None
    except Exception as e:
        log.exception(f"Unexpected failure for {category} ({url})")
        op.record_failure(category, url, "unexpected_error", str(e))
        return None


async def run_live_evaluation(
    selected_categories: Optional[List[str]] = None,
    delay_seconds: float = 30.0,
    resume: bool = False,
    state_file: Path = DEFAULT_STATE_JSON,
) -> tuple[List[EvaluationResult], OperationalMetrics]:
    """Run sequential live pipeline evaluation with state persistence and TPM pacing."""
    settings = get_settings()
    op = OperationalMetrics()
    quality_results: List[EvaluationResult] = []
    completed_categories: set[str] = set()

    # Load existing state if resuming
    if resume and state_file.exists():
        try:
            saved_state = json.loads(state_file.read_text(encoding="utf-8"))
            op_data = saved_state.get("operational_metrics", {})
            op.total_attempts = op_data.get("total_attempts", 0)
            op.successful_analyses = op_data.get("successful_analyses", 0)
            op.website_access_failures = op_data.get("website_access_failures", 0)
            op.rate_limit_failures = op_data.get("rate_limit_failures", 0)
            op.payload_limit_failures = op_data.get("payload_limit_failures", 0)
            op.timeout_failures = op_data.get("timeout_failures", 0)
            op.other_failures = op_data.get("other_failures", 0)
            op.failure_log = op_data.get("failure_log", [])

            for item in saved_state.get("quality_results", []):
                ev = EvaluationResult(category=item["category"], url=item["url"], is_fixture=False)
                ev.sample_size = item.get("sample_size", 1)
                ev.scores = item.get("scores", {})
                ev.checks = item.get("checks", {})
                ev.details = item.get("details", {})
                ev.needs_manual_review = item.get("needs_manual_review", [])
                ev.ground_truth_matched = item.get("ground_truth_matched", {})
                quality_results.append(ev)
                completed_categories.add(item["category"])
            log.info(f"Resumed from {state_file}: {len(completed_categories)} categories already completed.")
        except Exception as exc:
            log.warning(f"Could not parse state file {state_file}: {exc}; starting fresh.")

    categories_to_run = [c for c in ALL_CATEGORIES if not selected_categories or c in selected_categories]

    async with httpx.AsyncClient() as client:
        service = build_service(settings, client)
        service.cache = TTLCache(0, 0)  # always fresh evaluation

        for idx, category in enumerate(categories_to_run):
            if category in completed_categories:
                log.info(f"Skipping already completed category: {category}")
                continue

            conf = TARGET_CONFIG.get(category, {})
            primary_url = conf.get("primary_url")
            fallback_url = conf.get("fallback_url")

            log.info(f"[{idx+1}/{len(categories_to_run)}] Evaluating Category: {category}")
            eval_res = await execute_target_analysis(service, category, primary_url, op)

            # If primary was blocked by bot protection (403) and an accessible alternative exists, run alternative
            if eval_res is None and fallback_url:
                last_failure = op.failure_log[-1] if op.failure_log else {}
                if "403" in last_failure.get("failure_type", "") or "blocked" in last_failure.get("failure_type", ""):
                    log.info(f"Primary site blocked by WAF. Trying accessible alternative for {category}: {fallback_url}")
                    await asyncio.sleep(2.0)
                    eval_res = await execute_target_analysis(service, category, fallback_url, op)

            if eval_res is not None:
                quality_results.append(eval_res)
                completed_categories.add(category)

            # Persist state after each category
            persist_state(state_file, op, quality_results)

            # Cooldown pacing between categories if more remain
            if idx < len(categories_to_run) - 1:
                log.info(f"Pacing cooldown: sleeping {delay_seconds:.1f}s to respect provider TPM limits...")
                await asyncio.sleep(delay_seconds)

    return quality_results, op


def persist_state(path: Path, op: OperationalMetrics, quality_results: List[EvaluationResult]) -> None:
    """Save progress after each category so interrupted runs retain completed work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "operational_metrics": op.to_dict(),
        "quality_results": [r.to_dict() for r in quality_results],
    }
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def generate_benchmark_report(
    quality_results: List[EvaluationResult],
    op: OperationalMetrics,
    fixture_results: Optional[List[EvaluationResult]] = None,
) -> str:
    """Generate Markdown report strictly separating operational metrics from quality scores."""
    lines = [
        "# Benchmark Integrity & Live Evaluation Report (Phase 3.4.1)",
        "",
        f"**Timestamp:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  ",
        "**LLM Provider Tested:** Groq (`openai/gpt-oss-120b`)  ",
        "**TPM Limit Configured:** 8,000 Tokens/Minute  ",
        "",
        "---",
        "",
        "## 1. Operational Reliability Metrics",
        "",
        "Operational metrics measure infrastructure, network transport, rate limits, and provider payload boundaries across all requests.",
        "",
        format_operational_table(op),
        "",
    ]

    if op.failure_log:
        lines.extend([
            "### Detailed Operational Failure Log",
            "",
            "| Category | Target URL | Failure Type | Error Code | Message |",
            "|---|---|---|---|---|",
        ])
        for f in op.failure_log:
            lines.append(f"| {f['category']} | `{f['url']}` | **{f['failure_type']}** | `{f['error_code']}` | {f['message']} |")
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 2. Analysis Quality Scores (Successful Analyses Only)",
        "",
        "> [!IMPORTANT]",
        "> Quality metrics evaluate **only successfully completed analyses** ($n \\ge 1$).",
        "> Infrastructure failures (e.g. HTTP 403 WAF blocks or provider 429 rate limits) are tracked strictly under **Operational Metrics** and are **never converted into 0% quality scores**.",
        "> Categories with no successful run report **N/A**.",
        "",
        format_quality_summary(quality_results, ALL_CATEGORIES),
        "",
    ])

    if quality_results:
        successful_scores = [r.quality_score for r in quality_results]
        avg_quality = sum(successful_scores) / len(successful_scores)
        lines.append(f"**Mean Quality Score on Successful Analyses:** `{avg_quality:.1%}` (across {len(quality_results)} successful categories)\n")

        lines.extend([
            "### Items Requiring Human Judgment (`needs_manual_review`)",
            "",
        ])
        for r in quality_results:
            if r.needs_manual_review:
                lines.append(f"#### {r.category} (`{r.url}`)")
                for item in r.needs_manual_review:
                    lines.append(f"- [ ] {item}")
                lines.append("")

    if fixture_results:
        lines.extend([
            "---",
            "",
            "## 3. Offline Fixture Consistency Validation",
            "",
            "> [!NOTE]",
            "> **Methodological Limitation Notice:**",
            "> The offline benchmark validates schema conformity, quotation extraction invariants, and evaluator consistency against prepared test fixtures (`benchmarks/categories_fixtures.json`).",
            "> **It does NOT establish real-world model accuracy.** Real-world model quality is measured strictly in Section 2.",
            "",
            format_fixture_consistency_summary(fixture_results),
            "",
        ])

    lines.extend([
        "---",
        "",
        "## 4. How to Reproduce",
        "",
        "```bash",
        "# 1. Run offline fixture consistency validation:",
        "python -m scripts.run_benchmark_8_categories --offline",
        "",
        "# 2. Run live evaluation for a single category (e.g. SaaS):",
        "python -m scripts.run_benchmark_8_categories --live --category SaaS",
        "",
        "# 3. Run live evaluation for selected categories with 30s delay:",
        "python -m scripts.run_benchmark_8_categories --live --categories SaaS \"Digital Marketing\" Developer Platform --delay 30",
        "",
        "# 4. Resume interrupted live benchmark without repeating completed categories:",
        "python -m scripts.run_benchmark_8_categories --live --resume --delay 30",
        "```",
    ])

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Run 8-Category Benchmark Evaluation (Phase 3.4.1)")
    parser.add_argument("--offline", action="store_true", help="Run offline fixture consistency validation")
    parser.add_argument("--live", action="store_true", help="Run live pipeline evaluation against real websites")
    parser.add_argument("--category", type=str, default=None, help="Run single category (e.g. SaaS)")
    parser.add_argument("--categories", nargs="+", default=None, help="Run selected categories")
    parser.add_argument("--delay", type=float, default=30.0, help="Seconds to sleep between live requests (default: 30.0)")
    parser.add_argument("--resume", action="store_true", help="Resume from existing state file")
    parser.add_argument("--fixtures", type=Path, default=BENCHMARK_FIXTURES_PATH, help="Path to fixtures JSON")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_JSON, help="Path to output JSON")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_MD, help="Path to output markdown report")
    args = parser.parse_args()

    selected = [args.category] if args.category else args.categories

    if not args.offline and not args.live:
        args.offline = True

    fixture_results: Optional[List[EvaluationResult]] = None
    quality_results: List[EvaluationResult] = []
    op = OperationalMetrics()

    if args.offline:
        log.info("Running OFFLINE fixture consistency validation...")
        fixture_results = run_offline_evaluation(args.fixtures)
        print("\n" + format_fixture_consistency_summary(fixture_results) + "\n")
        # Load saved live state if present to preserve operational metrics in report
        if DEFAULT_STATE_JSON.exists():
            try:
                saved = json.loads(DEFAULT_STATE_JSON.read_text(encoding="utf-8"))
                op_data = saved.get("operational_metrics", {})
                op.total_attempts = op_data.get("total_attempts", 0)
                op.successful_analyses = op_data.get("successful_analyses", 0)
                op.website_access_failures = op_data.get("website_access_failures", 0)
                op.rate_limit_failures = op_data.get("rate_limit_failures", 0)
                op.payload_limit_failures = op_data.get("payload_limit_failures", 0)
                op.timeout_failures = op_data.get("timeout_failures", 0)
                op.other_failures = op_data.get("other_failures", 0)
                op.failure_log = op_data.get("failure_log", [])
                for item in saved.get("quality_results", []):
                    ev = EvaluationResult(category=item["category"], url=item["url"], is_fixture=False)
                    ev.sample_size = item.get("sample_size", 1)
                    ev.scores = item.get("scores", {})
                    ev.checks = item.get("checks", {})
                    ev.details = item.get("details", {})
                    ev.needs_manual_review = item.get("needs_manual_review", [])
                    ev.ground_truth_matched = item.get("ground_truth_matched", {})
                    quality_results.append(ev)
            except Exception:
                pass

    if args.live:
        log.info("Running LIVE evaluation with Groq...")
        quality_results, op = asyncio.run(
            run_live_evaluation(
                selected_categories=selected,
                delay_seconds=args.delay,
                resume=args.resume,
            )
        )
        print("\n--- OPERATIONAL RELIABILITY ---")
        print(format_operational_table(op))
        print("\n--- ANALYSIS QUALITY (SUCCESSFUL ANALYSES) ---")
        print(format_quality_summary(quality_results, ALL_CATEGORIES) + "\n")

    # Always ensure fixture consistency validation is available for Section 3
    if fixture_results is None and args.fixtures.exists():
        fixture_results = run_offline_evaluation(args.fixtures)

    # Generate Markdown report
    report_md = generate_benchmark_report(quality_results, op, fixture_results)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report_md, encoding="utf-8")
    log.info(f"Saved benchmark report to {args.report}")

    # Save output JSON
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out_data = {
        "operational_metrics": op.to_dict(),
        "quality_results": [r.to_dict() for r in quality_results],
        "fixture_consistency_results": [r.to_dict() for r in (fixture_results or [])],
    }
    args.out.write_text(json.dumps(out_data, indent=2), encoding="utf-8")
    log.info(f"Saved evaluation JSON to {args.out}")


if __name__ == "__main__":
    main()
