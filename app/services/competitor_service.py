"""Competitor post-processing and relevance assessment.

The LLM suggests competitor candidates from its own knowledge.
Each candidate is verified through two separate dimensions:
1. Reachability & Name Verification: Does the suggested domain resolve, and does it mention the name?
2. Market Relevance Assessment: Evaluates actual fetched competitor page content against the analyzed
   business across category overlap, offering overlap, audience overlap, and geographic context.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from app.config import Settings
from app.errors import AppError
from app.schemas import (
    Competitor,
    CompetitorRelevance,
    CompetitorType,
    Evidence,
    LLMCompetitor,
    RelevanceStatus,
)
from app.services.website_extractor import PageContent, SafeFetcher, parse_page
from app.utils.validators import normalize_url

log = logging.getLogger("link_analysis.competitors")
_TOKEN = re.compile(r"[a-z0-9]{3,}")
_GENERIC = {
    "inc", "ltd", "llc", "corp", "the", "and", "company", "group", "technologies",
    "solutions", "services", "software", "international", "global", "holdings",
}

# Indicators for clinical/medical healthcare vs packaged consumer goods
_PRODUCT_MANUFACTURER_KEYWORDS = {
    "toothpaste", "toothbrush", "mouthwash", "whitening strips", "oral care products",
    "buy now", "add to cart", "store locator", "consumer products", "retailers", "shop products",
}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _name_tokens(name: str) -> set[str]:
    return {t for t in _TOKEN.findall(name.lower()) if t not in _GENERIC}


def _find_best_excerpt(page: PageContent, keywords: list[str]) -> str | None:
    """Find a clean, verbatim quote from the page matching any relevant keyword."""
    if not keywords:
        return None
    search_targets: list[str] = []
    if page.meta_description:
        search_targets.append(page.meta_description)
    if page.title:
        search_targets.append(page.title)
    search_targets.extend(page.h1)
    search_targets.extend(page.headings)
    search_targets.extend(page.blocks[:15])

    for target in search_targets:
        clean = re.sub(r"^#+\s*", "", target).strip()
        lower = clean.lower()
        if len(clean) >= 15:
            for kw in keywords:
                if len(kw) >= 3 and kw in lower:
                    return clean[:200]
    return None


def validate_competitor_evidence(evidence: list[Evidence], page: PageContent) -> list[Evidence]:
    """Filter out any evidence quotes that do not appear verbatim in the fetched competitor page."""
    from app.services.grounding import verify_and_canonicalize_evidence

    verified_list: list[Evidence] = []
    for ev in evidence:
        verified = verify_and_canonicalize_evidence(ev, [page])
        if verified is not None:
            verified_list.append(verified)
    return verified_list


def _evaluate_competitor_relevance(
    comp: LLMCompetitor,
    page: PageContent,
    *,
    brand_name: str = "",
    brand_category: str = "",
    services: list[Any] | None = None,
    geographic_focus: str | None = None,
    target_audience: list[str] | None = None,
) -> CompetitorRelevance:
    """Assess market relevance between analyzed business and candidate competitor based on fetched content."""
    # Check for insufficient readable content (< 30 words)
    if page.word_count < 30:
        return CompetitorRelevance(
            status="insufficient_evidence",
            competitor_type="unknown",
            signals=[],
            explanation="Candidate website contains insufficient readable content to assess market relevance.",
            evidence=[],
        )

    haystack_parts = [
        page.title or "",
        page.meta_description or "",
        page.meta_keywords or "",
        " ".join(page.h1),
        " ".join(page.headings),
        " ".join(page.blocks[:25]),
    ]
    if page.footer_text:
        haystack_parts.append(page.footer_text)
    comp_text = " ".join(haystack_parts).lower()

    signals: list[str] = []
    evidence_list: list[Evidence] = []
    category_lower = (brand_category or "").lower()

    # 1. Clinical practice vs Consumer manufacturer mismatch
    # (e.g. Local dental clinic vs Toothpaste manufacturer)
    is_brand_clinical = any(
        k in category_lower
        for k in ("dentist", "dental clinic", "clinic", "medical practice", "healthcare practice")
    )
    if not is_brand_clinical and services:
        service_texts = " ".join(getattr(s, "name", str(s)) for s in services).lower()
        if any(k in service_texts for k in ("dental", "teeth cleaning", "root canal", "invisalign", "clinic")):
            is_brand_clinical = True

    comp_has_manufacturer = any(k in comp_text for k in _PRODUCT_MANUFACTURER_KEYWORDS)
    comp_has_clinical = any(
        k in comp_text
        for k in ("clinic", "practice", "patient", "patients", "appointment", "consultation", "dental clinic")
    )

    if is_brand_clinical and comp_has_manufacturer and not comp_has_clinical:
        quote = _find_best_excerpt(page, ["toothpaste", "toothbrush", "oral care", "products", "smiles"])
        if quote:
            evidence_list.append(Evidence(quote=quote, source_url=page.url))
        return CompetitorRelevance(
            status="category_mismatch",
            competitor_type="substitute",
            signals=[],
            explanation="Candidate is a consumer oral care product manufacturer, not a dental clinic providing clinical dental services.",
            evidence=evidence_list,
        )

    # 2. Category overlap
    cat_words = [w for w in re.findall(r"[a-z0-9]{4,}", category_lower) if w not in _GENERIC]
    if cat_words:
        matched_cat_words = [w for w in cat_words if re.search(r"\b" + re.escape(w) + r"\b", comp_text)]
        if len(matched_cat_words) >= 1:
            signals.append("category_overlap")

    # 3. Offering overlap
    service_names: list[str] = []
    if services:
        for s in services:
            s_name = getattr(s, "name", "") if hasattr(s, "name") else str(s)
            if s_name:
                service_names.append(s_name)

    offering_matches = 0
    matching_offering_terms: list[str] = []
    for s_name in service_names:
        norm_s = re.sub(r"\b(?:services|solutions|management|offerings)\b", "", s_name.lower()).strip()
        s_words = [w for w in re.findall(r"[a-z0-9]{4,}", norm_s) if w not in _GENERIC]
        if s_words:
            if all(re.search(r"\b" + re.escape(w) + r"\b", comp_text) for w in s_words):
                offering_matches += 1
                matching_offering_terms.extend(s_words)
            elif len(s_words) >= 2 and any(re.search(r"\b" + re.escape(w) + r"\b", comp_text) for w in s_words):
                matching_offering_terms.extend(s_words)

    if offering_matches >= 1 or len(set(matching_offering_terms)) >= 2:
        signals.append("offering_overlap")

    # 4. Audience overlap
    audience_tokens = set()
    if target_audience:
        for a in target_audience:
            audience_tokens.update(re.findall(r"[a-z0-9]{4,}", a.lower()))
    common_audience_keywords = {"business", "businesses", "founders", "startups", "enterprises", "developers", "teams", "smb", "smbs", "clients"}
    relevant_aud = (audience_tokens & common_audience_keywords) or {"business", "developers", "clients"}
    if any(re.search(r"\b" + re.escape(w) + r"\b", comp_text) for w in relevant_aud if w in comp_text):
        signals.append("audience_overlap")

    # 5. Geographic overlap (treated cautiously: missing local evidence is treated as uncertainty)
    if geographic_focus and geographic_focus.strip():
        geo_terms = [w for w in re.findall(r"[a-z0-9]{3,}", geographic_focus.lower()) if w not in _GENERIC]
        if any(re.search(r"\b" + re.escape(w) + r"\b", comp_text) for w in geo_terms):
            signals.append("geographic_overlap")

    # Extract supporting evidence from competitor page
    best_quote = _find_best_excerpt(page, matching_offering_terms or cat_words or [comp.name.lower()])
    if best_quote:
        evidence_list.append(Evidence(quote=best_quote, source_url=page.url))

    # Sanitize evidence: only keep quotes that verifiably exist on the fetched page
    evidence_list = validate_competitor_evidence(evidence_list, page)

    # Determine status & type
    if "offering_overlap" in signals and ("category_overlap" in signals or len(signals) >= 2):
        status: RelevanceStatus = "likely_relevant"
        comp_type: CompetitorType = "direct"
        explanation = f"Both websites describe overlapping offerings and market focus in {brand_category or 'the sector'}."
    elif "category_overlap" in signals or len(signals) >= 1:
        status: RelevanceStatus = "possible"
        comp_type: CompetitorType = "indirect"
        explanation = "Candidate operates in a related category, but specific product or service offerings show partial or indirect overlap."
    else:
        status: RelevanceStatus = "low_relevance"
        comp_type: CompetitorType = "unknown"
        explanation = "Candidate website does not describe products or services overlapping with the analyzed business."

    return CompetitorRelevance(
        status=status,
        competitor_type=comp_type,
        signals=signals,
        explanation=explanation,
        evidence=evidence_list[:2],
    )


class CompetitorService:
    def __init__(self, fetcher: SafeFetcher, settings: Settings):
        self.fetcher = fetcher
        self.settings = settings

    async def verify(
        self,
        suggestions: list[LLMCompetitor],
        brand_url: str,
        *,
        brand_name: str = "",
        brand_category: str = "",
        services: list[Any] | None = None,
        geographic_focus: str | None = None,
        target_audience: list[str] | None = None,
    ) -> list[Competitor]:
        brand_host = _host(brand_url)
        unique: list[LLMCompetitor] = []
        seen: set[str] = set()
        for comp in suggestions:
            key = comp.name.strip().lower()
            comp_host = _host(comp.website) if comp.website and "." in comp.website else ""
            if not key or key in seen or (comp_host and comp_host == brand_host):
                continue                              # duplicate, or the LLM listed the brand itself
            seen.add(key)
            unique.append(comp)
        unique = unique[:6]

        if not self.settings.verify_competitors:
            return [Competitor(**c.model_dump(), verification="not_checked") for c in unique]

        sem = asyncio.Semaphore(4)

        async def check(comp: LLMCompetitor) -> Competitor:
            async with sem:
                verification, title, relevance = await self._check_one(
                    comp,
                    brand_name=brand_name,
                    brand_category=brand_category,
                    services=services,
                    geographic_focus=geographic_focus,
                    target_audience=target_audience,
                )
            return Competitor(
                **comp.model_dump(),
                verification=verification,
                verified_page_title=title,
                relevance=relevance,
            )

        return list(await asyncio.gather(*(check(c) for c in unique)))

    async def _check_one(
        self,
        comp: LLMCompetitor,
        *,
        brand_name: str = "",
        brand_category: str = "",
        services: list[Any] | None = None,
        geographic_focus: str | None = None,
        target_audience: list[str] | None = None,
    ) -> tuple[str, str | None, CompetitorRelevance]:
        if not comp.website:
            return (
                "no_website_provided",
                None,
                CompetitorRelevance(
                    status="not_checked",
                    competitor_type="unknown",
                    explanation="No website URL provided to assess competitor market relevance.",
                ),
            )
        try:
            url = normalize_url(comp.website, allow_private=self.settings.allow_private_urls)
        except AppError:
            return (
                "invalid_website_url",
                None,
                CompetitorRelevance(
                    status="not_checked",
                    competitor_type="unknown",
                    explanation="Invalid competitor website URL format.",
                ),
            )
        try:
            fetched = await self.fetcher.fetch(url, max_bytes=250_000, timeout=6)
        except AppError as exc:
            # A site that blocks bots (403/429) still *exists*; treat only hard failures as unreachable.
            if exc.code == "website_access_denied":
                return (
                    "live_site_name_not_matched",
                    None,
                    CompetitorRelevance(
                        status="insufficient_evidence",
                        competitor_type="unknown",
                        explanation="Candidate website refused automated access (HTTP 403/429); relevance could not be verified.",
                    ),
                )
            log.info("competitor %s unreachable: %s", comp.name, exc.code)
            return (
                "unreachable",
                None,
                CompetitorRelevance(
                    status="not_checked",
                    competitor_type="unknown",
                    explanation="Candidate website was unreachable; market relevance could not be verified.",
                ),
            )
        except Exception:  # pragma: no cover - defensive
            return (
                "unreachable",
                None,
                CompetitorRelevance(
                    status="not_checked",
                    competitor_type="unknown",
                    explanation="Candidate website connection failed.",
                ),
            )

        page = parse_page(fetched.body, fetched.url)
        # The domain itself is NOT evidence (the LLM supplied it); the page content must mention the name.
        haystack = " ".join(filter(None, [page.title, page.og.get("og:site_name"), page.meta_description, " ".join(page.h1)])).lower()
        tokens = _name_tokens(comp.name)
        matched = bool(tokens) and any(t in haystack for t in tokens)
        verification = "verified_live_site" if matched else "live_site_name_not_matched"

        relevance = _evaluate_competitor_relevance(
            comp,
            page,
            brand_name=brand_name,
            brand_category=brand_category,
            services=services,
            geographic_focus=geographic_focus,
            target_audience=target_audience,
        )

        return verification, page.title, relevance
