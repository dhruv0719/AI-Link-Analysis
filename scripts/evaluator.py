"""Evaluation framework for AI Website Link Analysis API.

Distinguishes between:
1. Operational Reliability: Network access, rate limits, payload limits, timeouts, and success rate.
2. Analysis Quality: Evaluated exclusively on successfully completed analyses against objective criteria and ground truth.
3. Offline Fixture Consistency: Validates prepared fixtures for schema conformity and pipeline invariants.

Where human judgment is required, items are explicitly marked with `needs_manual_review`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, get_args

from app.schemas import LinkAnalysisResponse, Platform
from app.services.grounding import normalize_text_for_matching
from app.services.website_extractor import PageContent

ALL_SUPPORTED_PLATFORMS = get_args(Platform)
GROUND_TRUTH_PATH = Path("benchmarks/ground_truth.json")


@dataclass
class OperationalMetrics:
    total_attempts: int = 0
    successful_analyses: int = 0
    website_access_failures: int = 0   # HTTP 403, 401, bot protection
    rate_limit_failures: int = 0       # HTTP 429
    payload_limit_failures: int = 0    # HTTP 413
    timeout_failures: int = 0          # HTTP 504
    other_failures: int = 0
    failure_log: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return (self.successful_analyses / self.total_attempts) if self.total_attempts > 0 else 0.0

    def record_success(self, category: str, url: str) -> None:
        self.total_attempts += 1
        self.successful_analyses += 1

    def record_failure(self, category: str, url: str, error_code: str, message: str) -> None:
        self.total_attempts += 1
        err_lower = f"{error_code} {message}".lower()
        if "403" in err_lower or "access_denied" in err_lower or "blocked" in err_lower:
            self.website_access_failures += 1
            failure_type = "website_access_blocked (403)"
        elif "429" in err_lower or "rate_limit" in err_lower or "too many requests" in err_lower:
            self.rate_limit_failures += 1
            failure_type = "provider_rate_limit (429)"
        elif "413" in err_lower or "payload_too_large" in err_lower or "payload too large" in err_lower or "tokens per minute" in err_lower:
            self.payload_limit_failures += 1
            failure_type = "provider_payload_limit (413)"
        elif "504" in err_lower or "timeout" in err_lower:
            self.timeout_failures += 1
            failure_type = "timeout (504)"
        else:
            self.other_failures += 1
            failure_type = f"other ({error_code})"

        self.failure_log.append({
            "category": category,
            "url": url,
            "error_code": error_code,
            "message": message,
            "failure_type": failure_type,
        })

    def to_dict(self) -> dict:
        return {
            "total_attempts": self.total_attempts,
            "successful_analyses": self.successful_analyses,
            "website_access_failures": self.website_access_failures,
            "rate_limit_failures": self.rate_limit_failures,
            "payload_limit_failures": self.payload_limit_failures,
            "timeout_failures": self.timeout_failures,
            "other_failures": self.other_failures,
            "success_rate": round(self.success_rate, 4),
            "failure_log": self.failure_log,
        }


class EvaluationResult:
    def __init__(self, category: str, url: str, is_fixture: bool = False):
        self.category = category
        self.url = url
        self.is_fixture = is_fixture
        self.sample_size: int = 1
        self.scores: Dict[str, float] = {}
        self.checks: Dict[str, bool] = {}
        self.details: Dict[str, Any] = {}
        self.needs_manual_review: List[str] = []
        self.ground_truth_matched: Dict[str, bool] = {}

    @property
    def requires_human_review(self) -> List[str]:
        return self.needs_manual_review

    @property
    def quality_score(self) -> float:
        if not self.scores:
            return 0.0
        return sum(self.scores.values()) / len(self.scores)

    @property
    def fixture_consistency_score(self) -> float:
        return self.quality_score

    @property
    def overall_score(self) -> float:
        # Kept for backwards compatibility
        return self.quality_score

    def to_dict(self) -> dict:
        score_key = "fixture_consistency_score" if self.is_fixture else "quality_score"
        return {
            "category": self.category,
            "url": self.url,
            "is_fixture": self.is_fixture,
            "sample_size": self.sample_size,
            score_key: round(self.quality_score, 2),
            "overall_score": round(self.quality_score, 2),
            "scores": {k: round(v, 2) for k, v in self.scores.items()},
            "checks": self.checks,
            "ground_truth_matched": self.ground_truth_matched,
            "details": self.details,
            "needs_manual_review": self.needs_manual_review,
        }


def load_ground_truth() -> dict:
    if GROUND_TRUTH_PATH.exists():
        try:
            return json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def evaluate_analysis(
    data: dict,
    category: str,
    pages: Optional[List[PageContent]] = None,
    is_fixture: bool = False,
    ground_truth: Optional[dict] = None,
) -> EvaluationResult:
    """Evaluate an analysis response dict against objective quality criteria and ground truth."""
    url = data.get("website", "")
    ev = EvaluationResult(category=category, url=url, is_fixture=is_fixture)
    brand = data.get("brand", {})
    analysis = data.get("analysis", {})
    meta = data.get("meta", {})

    if ground_truth is None:
        gt_data = load_ground_truth()
        ground_truth = gt_data.get(category, {})

    expected_facts = ground_truth.get("expected_facts", {}) if ground_truth else {}
    manual_criteria = ground_truth.get("human_review_criteria", []) if ground_truth else []

    # -------------------------------------------------------------------------
    # Dimension A: Website Understanding (0.0 to 1.0)
    # -------------------------------------------------------------------------
    brand_name = brand.get("name", "").strip()
    brand_cat = brand.get("category", "").strip()
    has_brand_name = bool(brand_name and len(brand_name) >= 2)
    has_category = bool(brand_cat and len(brand_cat) >= 3)
    has_summary = bool(brand.get("summary") and len(brand["summary"].strip()) >= 20)
    has_target_audience = bool(analysis.get("target_audience", {}).get("primary_audience"))

    # Ground truth brand/category match
    if expected_facts:
        expected_brands = [b.lower() for b in expected_facts.get("expected_brand_names", [])]
        brand_matched = any(b in brand_name.lower() or brand_name.lower() in b for b in expected_brands) if expected_brands else True
        ev.ground_truth_matched["brand_name_accurate"] = brand_matched
    else:
        brand_matched = True

    ev.checks["brand_identified"] = has_brand_name
    ev.checks["category_identified"] = has_category
    ev.checks["summary_adequate"] = has_summary
    ev.checks["audience_identified"] = has_target_audience

    brand_score = sum([has_brand_name, has_category, has_summary, has_target_audience, (1.0 if brand_matched else 0.5)]) / 5.0
    ev.scores["website_understanding"] = brand_score

    # -------------------------------------------------------------------------
    # Dimension B: Evidence Grounding & Provenance (0.0 to 1.0)
    # -------------------------------------------------------------------------
    services = analysis.get("top_services", [])
    verified_services = [s for s in services if s.get("source") == "website"]
    inferred_services = [s for s in services if s.get("source") == "inferred"]

    valid_evidence_quotes = 0
    total_evidence_quotes = 0
    for s in verified_services:
        evidence_list = s.get("evidence", [])
        for item in evidence_list:
            total_evidence_quotes += 1
            quote = item.get("quote", "")
            source_url = item.get("source_url", "")
            if quote and source_url:
                if pages:
                    matching_pages = [p for p in pages if source_url.rstrip("/").lower() in p.url.rstrip("/").lower()]
                    if matching_pages:
                        page_text = " ".join(matching_pages[0].blocks + [matching_pages[0].title or ""])
                        if normalize_text_for_matching(quote) in normalize_text_for_matching(page_text):
                            valid_evidence_quotes += 1
                else:
                    if len(quote) >= 5 and source_url.startswith("http"):
                        valid_evidence_quotes += 1

    grounding_ratio = (valid_evidence_quotes / total_evidence_quotes) if total_evidence_quotes > 0 else (1.0 if not verified_services else 0.0)
    ev.checks["services_provenance_labeled"] = all(s.get("source") in ("website", "inferred") for s in services)
    ev.checks["evidence_quotes_grounded"] = grounding_ratio >= 0.8
    ev.scores["grounding_accuracy"] = (grounding_ratio + (1.0 if ev.checks["services_provenance_labeled"] else 0.0)) / 2.0

    # -------------------------------------------------------------------------
    # Dimension C: Competitor Intelligence & Comparison Consistency (0.0 to 1.0)
    # -------------------------------------------------------------------------
    comp_analysis = analysis.get("competitor_analysis", {})
    competitors = comp_analysis.get("competitors", [])
    comparison = comp_analysis.get("comparison", [])

    relevance_checked = all("relevance" in c for c in competitors) if competitors else True
    eligible_names = {
        c.get("name", "").strip().lower()
        for c in competitors
        if c.get("relevance", {}).get("status") in ("likely_relevant", "possible")
    }
    comparison_compliant = True
    your_brand_present = False
    for row in comparison:
        if row.get("role") == "your_brand":
            your_brand_present = True
        else:
            row_name = row.get("brand_name", "").strip().lower()
            if row_name not in eligible_names:
                comparison_compliant = False

    ev.checks["competitor_relevance_evaluated"] = relevance_checked
    ev.checks["comparison_eligibility_enforced"] = comparison_compliant
    ev.checks["your_brand_comparison_retained"] = your_brand_present or len(comparison) == 0

    # Check against known invalid competitors in ground truth
    if expected_facts:
        invalid_list = [inv.lower() for inv in expected_facts.get("known_invalid_competitors", [])]
        found_invalid = any(any(inv in c.get("name", "").lower() for inv in invalid_list) for c in competitors)
        ev.ground_truth_matched["no_disqualified_competitors"] = not found_invalid
    else:
        found_invalid = False

    comp_score = sum([relevance_checked, comparison_compliant, (your_brand_present or len(comparison) == 0), (0.5 if not found_invalid else 0.0)]) / 3.5
    ev.scores["competitor_intelligence"] = min(comp_score, 1.0)

    # -------------------------------------------------------------------------
    # Dimension D: Search Query & Platform Coverage (0.0 to 1.0)
    # -------------------------------------------------------------------------
    queries = analysis.get("target_search_queries", [])
    has_five_queries = len(queries) == 5
    covered_platforms = {p for q in queries for p in q.get("platforms", [])}
    all_five_covered = set(ALL_SUPPORTED_PLATFORMS).issubset(covered_platforms)
    has_platform_variants = all(bool(q.get("platform_queries")) for q in queries)

    ev.checks["exactly_five_queries"] = has_five_queries
    ev.checks["all_five_platforms_covered"] = all_five_covered
    ev.checks["platform_variants_present"] = has_platform_variants

    query_score = sum([has_five_queries, all_five_covered, has_platform_variants]) / 3.0
    ev.scores["search_query_quality"] = query_score

    # -------------------------------------------------------------------------
    # Dimension E: SEO Quality (0.0 to 1.0)
    # -------------------------------------------------------------------------
    seo = analysis.get("seo_improvements", {})
    rec_title = seo.get("recommended_title", "")
    rec_meta = seo.get("meta_description", "")
    title_ok = 10 <= len(rec_title) <= 65
    meta_ok = 70 <= len(rec_meta) <= 165
    has_checks = bool(seo.get("on_page_checks"))

    ev.checks["seo_title_bounded"] = title_ok
    ev.checks["seo_meta_bounded"] = meta_ok
    ev.checks["on_page_checks_computed"] = has_checks

    seo_score = sum([title_ok, meta_ok, has_checks]) / 3.0
    ev.scores["seo_quality"] = seo_score

    # -------------------------------------------------------------------------
    # Dimension F: Services & Audience Quality (0.0 to 1.0)
    # -------------------------------------------------------------------------
    services_bounded = 0 <= len(services) <= 5
    audience = analysis.get("target_audience", {})
    has_needs = bool(audience.get("customer_needs"))
    has_segments = bool(audience.get("recommended_marketing_segments"))

    ev.checks["services_bounded_0_to_5"] = services_bounded
    ev.checks["audience_needs_identified"] = has_needs
    ev.checks["audience_segments_identified"] = has_segments

    service_aud_score = sum([services_bounded, has_needs, has_segments]) / 3.0
    ev.scores["services_and_audience"] = service_aud_score

    # Human review triggers for subjective quality
    for crit in manual_criteria:
        ev.needs_manual_review.append(crit)

    if len(competitors) == 0:
        ev.needs_manual_review.append("Zero competitors returned; verify if market has identifiable online competitors.")

    ev.details = {
        "services_count": len(services),
        "verified_services": len(verified_services),
        "inferred_services": len(inferred_services),
        "competitors_count": len(competitors),
        "comparison_rows": len(comparison),
        "platforms_covered": sorted(list(covered_platforms)),
        "llm_timings_ms": meta.get("timings_ms", {}),
        "estimated_cost_usd": meta.get("estimated_cost_usd", 0.0),
    }

    return ev


def format_operational_table(op: OperationalMetrics) -> str:
    """Format operational metrics table."""
    lines = [
        "| Total Attempts | Successful Analyses | Website Access Failures (403) | Rate Limits (429) | Payload Limits (413) | Timeouts (504) | Other Failures | Success Rate |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {op.total_attempts} | {op.successful_analyses} | {op.website_access_failures} | {op.rate_limit_failures} | {op.payload_limit_failures} | {op.timeout_failures} | {op.other_failures} | **{op.success_rate:.1%}** |"
    ]
    return "\n".join(lines)


def format_quality_summary(results: List[EvaluationResult], all_categories: List[str]) -> str:
    """Render markdown summary of quality scores, reporting N/A for categories without successful runs."""
    results_by_cat = {r.category: r for r in results}
    lines = [
        "| Category | Target URL | Successful Runs (n) | Understanding | Grounding | Competitors | Search Queries | SEO | Services & Audience | Quality Score | Needs Manual Review |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for cat in all_categories:
        if cat in results_by_cat:
            r = results_by_cat[cat]
            review_flag = f"{len(r.needs_manual_review)} item(s)" if r.needs_manual_review else "None"
            lines.append(
                f"| **{r.category}** | `{r.url[:35]}` | {r.sample_size} | "
                f"{r.scores.get('website_understanding', 0):.0%} | "
                f"{r.scores.get('grounding_accuracy', 0):.0%} | "
                f"{r.scores.get('competitor_intelligence', 0):.0%} | "
                f"{r.scores.get('search_query_quality', 0):.0%} | "
                f"{r.scores.get('seo_quality', 0):.0%} | "
                f"{r.scores.get('services_and_audience', 0):.0%} | "
                f"**{r.quality_score:.0%}** | {review_flag} |"
            )
        else:
            lines.append(
                f"| **{cat}** | *(No successful run)* | 0 | N/A | N/A | N/A | N/A | N/A | N/A | **N/A** | Needs successful run |"
            )
    return "\n".join(lines)


def format_fixture_consistency_summary(results: List[EvaluationResult]) -> str:
    """Render summary table for offline fixture consistency validation."""
    lines = [
        "| Category | Fixture URL | Understanding | Grounding | Competitors | Search Queries | SEO | Services & Audience | Fixture Consistency Score |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| **{r.category}** | `{r.url[:35]}` | "
            f"{r.scores.get('website_understanding', 0):.0%} | "
            f"{r.scores.get('grounding_accuracy', 0):.0%} | "
            f"{r.scores.get('competitor_intelligence', 0):.0%} | "
            f"{r.scores.get('search_query_quality', 0):.0%} | "
            f"{r.scores.get('seo_quality', 0):.0%} | "
            f"{r.scores.get('services_and_audience', 0):.0%} | "
            f"**{r.fixture_consistency_score:.0%}** |"
        )
    return "\n".join(lines)


# Backwards compatibility alias
format_evaluation_summary = format_fixture_consistency_summary
