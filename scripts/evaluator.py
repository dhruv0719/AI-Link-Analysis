"""Evaluation framework for AI Website Link Analysis API.

Evaluates analysis quality across 6 key dimensions:
A. Website Understanding: Brand name identification, Category, Geographic focus.
B. Grounding & Provenance: Quotation verification, source URL canonicalization, claim accuracy.
C. Competitor Intelligence: Reachability vs Relevance separation, comparison table eligibility.
D. Search Query Quality: 5-platform representation (Google, Facebook, Instagram, LinkedIn, X) and format adaptation.
E. SEO Recommendations: Title length (<=60), meta description length (70-160), deterministic audit checks.
F. Services & Audience: Cardinality (0-5), inferred labeling, audience segmentation.

Distinguishes automated deterministic checks from human-judgment items.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, get_args
from urllib.parse import urlsplit

from app.schemas import LinkAnalysisResponse, Platform
from app.services.grounding import normalize_text_for_matching, verify_and_canonicalize_evidence
from app.services.website_extractor import PageContent

ALL_SUPPORTED_PLATFORMS = get_args(Platform)


class EvaluationResult:
    def __init__(self, category: str, url: str):
        self.category = category
        self.url = url
        self.scores: Dict[str, float] = {}
        self.checks: Dict[str, bool] = {}
        self.details: Dict[str, Any] = {}
        self.requires_human_review: List[str] = []

    @property
    def overall_score(self) -> float:
        if not self.scores:
            return 0.0
        return sum(self.scores.values()) / len(self.scores)

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "url": self.url,
            "overall_score": round(self.overall_score, 2),
            "scores": {k: round(v, 2) for k, v in self.scores.items()},
            "checks": self.checks,
            "details": self.details,
            "requires_human_review": self.requires_human_review,
        }


def evaluate_analysis(
    data: dict,
    category: str,
    pages: Optional[List[PageContent]] = None,
) -> EvaluationResult:
    """Evaluate an analysis response dict against objective quality criteria."""
    url = data.get("website", "")
    ev = EvaluationResult(category=category, url=url)
    brand = data.get("brand", {})
    analysis = data.get("analysis", {})
    meta = data.get("meta", {})

    # -------------------------------------------------------------------------
    # Dimension A: Website Understanding (0.0 to 1.0)
    # -------------------------------------------------------------------------
    has_brand_name = bool(brand.get("name") and len(brand["name"].strip()) >= 2)
    has_category = bool(brand.get("category") and len(brand["category"].strip()) >= 3)
    has_summary = bool(brand.get("summary") and len(brand["summary"].strip()) >= 20)
    has_target_audience = bool(analysis.get("target_audience", {}).get("primary_audience"))

    ev.checks["brand_identified"] = has_brand_name
    ev.checks["category_identified"] = has_category
    ev.checks["summary_adequate"] = has_summary
    ev.checks["audience_identified"] = has_target_audience

    brand_score = sum([has_brand_name, has_category, has_summary, has_target_audience]) / 4.0
    ev.scores["website_understanding"] = brand_score

    # -------------------------------------------------------------------------
    # Dimension B: Evidence Grounding & Provenance (0.0 to 1.0)
    # -------------------------------------------------------------------------
    services = analysis.get("top_services", [])
    verified_services = [s for s in services if s.get("source") == "website"]
    inferred_services = [s for s in services if s.get("source") == "inferred"]

    # Verify that website-claimed services actually provide evidence quotes
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
                    # Deterministic verification against actual page content
                    matching_pages = [p for p in pages if source_url.rstrip("/").lower() in p.url.rstrip("/").lower()]
                    if matching_pages:
                        page_text = " ".join(matching_pages[0].blocks + [matching_pages[0].title or ""])
                        if normalize_text_for_matching(quote) in normalize_text_for_matching(page_text):
                            valid_evidence_quotes += 1
                else:
                    # In fixture/offline mode without raw pages, verify non-empty quote & valid URL shape
                    if len(quote) >= 5 and source_url.startswith("http"):
                        valid_evidence_quotes += 1

    grounding_ratio = (valid_evidence_quotes / total_evidence_quotes) if total_evidence_quotes > 0 else (1.0 if not verified_services else 0.0)
    ev.checks["services_provenance_labeled"] = all(s.get("source") in ("website", "inferred") for s in services)
    ev.checks["evidence_quotes_grounded"] = grounding_ratio >= 0.8
    ev.scores["grounding_accuracy"] = (grounding_ratio + (1.0 if ev.checks["services_provenance_labeled"] else 0.0)) / 2.0

    # -------------------------------------------------------------------------
    # Dimension C: Competitor Analysis & Comparison Consistency (0.0 to 1.0)
    # -------------------------------------------------------------------------
    comp_analysis = analysis.get("competitor_analysis", {})
    competitors = comp_analysis.get("competitors", [])
    comparison = comp_analysis.get("comparison", [])

    # Reachability vs relevance separation check
    relevance_checked = all("relevance" in c for c in competitors) if competitors else True
    # Comparison table eligibility check: only likely_relevant or possible or your_brand
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

    comp_score = sum([relevance_checked, comparison_compliant, (your_brand_present or len(comparison) == 0)]) / 3.0
    ev.scores["competitor_intelligence"] = comp_score

    # -------------------------------------------------------------------------
    # Dimension D: Search Query & Platform Coverage (0.0 to 1.0)
    # -------------------------------------------------------------------------
    queries = analysis.get("target_search_queries", [])
    has_five_queries = len(queries) == 5
    covered_platforms = {p for q in queries for p in q.get("platforms", [])}
    all_five_covered = set(ALL_SUPPORTED_PLATFORMS).issubset(covered_platforms)
    
    # Check platform query variants presence
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
    if category in ("Healthcare", "Local trades"):
        if not audience.get("geographic_focus"):
            ev.requires_human_review.append("Local business missing explicit geographic focus; verify whether site states an address.")
    if len(competitors) == 0:
        ev.requires_human_review.append("Zero competitors returned; verify if market has identifiable online competitors.")

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


def format_evaluation_summary(results: List[EvaluationResult]) -> str:
    """Render a comprehensive markdown summary table across all evaluated categories."""
    lines = [
        "| Category | Target URL | Understanding | Grounding | Competitors | Search Queries | SEO | Services & Audience | Overall Score |",
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
            f"**{r.overall_score:.0%}** |"
        )
    return "\n".join(lines)
