"""Orchestrates the whole flow: validate -> cache -> extract -> LLM -> verify/enrich -> response."""
from __future__ import annotations

import logging
import time

from app.config import Settings
from app.errors import AppError
from app.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from app.schemas import (
    Analysis, CompetitorAnalysis, LinkAnalysisResponse, LLMAnalysis, Meta, Platform, SearchQuery, SeoImprovements,
    _derive_platform_variant,
)
from urllib.parse import urlsplit
from app.services.competitor_service import CompetitorService
from app.services.grounding import ground_brand, ground_service, ground_target_audience
from app.services.llm_service import LLMService
from app.services.website_extractor import SiteContent, WebsiteExtractor, seo_audit
from app.utils.cache import TTLCache
from app.utils.validators import normalize_url

log = logging.getLogger("link_analysis.service")
MIN_WORDS = 40
NO_VOLUME_NOTE = "No search-volume, ranking or traffic numbers are included: none were sourced from an API."
ALL_SUPPORTED_PLATFORMS: list[Platform] = ["Google", "Facebook", "Instagram", "LinkedIn", "X"]


def ensure_platform_coverage(
    queries: list[SearchQuery],
    *,
    brand_name: str = "",
    brand_category: str = "",
    geographic_focus: str | None = None,
) -> list[SearchQuery]:
    """Ensure that all 5 supported platforms are represented across the 5 search queries."""
    if not queries:
        return queries

    covered_platforms = {p for q in queries for p in q.platforms}
    missing_platforms = [p for p in ALL_SUPPORTED_PLATFORMS if p not in covered_platforms]
    if not missing_platforms:
        return queries

    for platform in missing_platforms:
        best_idx = 0
        best_score = -1

        for i, q in enumerate(queries):
            text = f"{q.query} {q.intent} {q.business_relevance} {q.target_audience}".lower()
            score = 0
            # Prefer queries that currently have fewer platforms assigned
            score += (5 - len(q.platforms)) * 3

            if platform == "Facebook":
                if any(w in text for w in ("local", "community", "small business", "sme", "near", "service", "client", "customer", "agency")):
                    score += 6
            elif platform == "Instagram":
                if any(w in text for w in ("design", "creative", "branding", "portfolio", "visual", "product", "studio", "lifestyle", "custom", "development")):
                    score += 6
            elif platform == "LinkedIn":
                if any(w in text for w in ("b2b", "enterprise", "agency", "vendor", "software", "saas", "professional", "founder", "developer", "solutions")):
                    score += 6
            elif platform == "X":
                if any(w in text for w in ("tech", "alternative", "tool", "privacy", "analytics", "developer", "discussion", "trend", "software")):
                    score += 6
            elif platform == "Google":
                if any(w in text for w in ("search", "find", "buy", "hire", "service")):
                    score += 6

            if score > best_score:
                best_score = score
                best_idx = i

        target_q = queries[best_idx]
        if platform not in target_q.platforms:
            target_q.platforms.append(platform)
            target_q.platform_queries.append(
                _derive_platform_variant(
                    platform,
                    target_q.query,
                    brand_name=brand_name,
                    category=brand_category,
                    location=geographic_focus,
                )
            )

    return queries


class AnalysisService:
    def __init__(self, settings: Settings, extractor: WebsiteExtractor, llm: LLMService,
                 competitors: CompetitorService, cache: TTLCache[LinkAnalysisResponse]):
        self.settings = settings
        self.extractor = extractor
        self.llm = llm
        self.competitors = competitors
        self.cache = cache
        self._schema = LLMAnalysis.model_json_schema()

    def _cache_key(self, url: str) -> str:
        provider, model = self.llm.primary_label
        return f"{PROMPT_VERSION}|{provider}|{model}|{url}"

    async def analyze(self, raw_url: str) -> LinkAnalysisResponse:
        t0 = time.perf_counter()
        url = normalize_url(raw_url, allow_private=self.settings.allow_private_urls)

        key = self._cache_key(url)
        if (hit := self.cache.get(key)) is not None:
            cached = hit.model_copy(deep=True)
            cached.meta.cached = True
            cached.meta.timings_ms = {"total": int((time.perf_counter() - t0) * 1000)}
            cached.meta.notes = [*cached.meta.notes, "Served from cache; no new LLM call was made."]
            return cached

        site = await self.extractor.extract_site(url)
        if site.word_count < MIN_WORDS and len((site.home.meta_description or "")) < 40:
            raise AppError(
                422, "insufficient_content",
                "Very little readable text was found. The site may be rendered with JavaScript only, "
                "be behind a login, or be an empty page.")

        t_llm = time.perf_counter()
        parsed, usage = await self.llm.generate_structured(
            system=SYSTEM_PROMPT,
            make_user_prompt=lambda errors: build_user_prompt(site.prompt_text, site.url, repair_errors=errors),
            schema=self._schema,
            parse=LLMAnalysis.model_validate,
        )
        llm_ms = int((time.perf_counter() - t_llm) * 1000)

        t_post = time.perf_counter()
        response = await self._assemble(parsed, site, usage)
        post_ms = int((time.perf_counter() - t_post) * 1000)

        response.meta.timings_ms = {
            "fetch_and_extract": site.fetch_ms, "llm": llm_ms, "verify_and_enrich": post_ms,
            "total": int((time.perf_counter() - t0) * 1000),
        }
        self.cache.set(key, response)
        log.info(
            "analysis ok url=%s provider=%s model=%s pages=%d in_tok=%d out_tok=%d cost=$%s llm_calls=%d repaired=%s timings=%s",
            site.url, usage.provider, usage.model, len(site.pages), usage.input_tokens + usage.cached_input_tokens,
            usage.output_tokens, usage.cost_usd, usage.calls, usage.repaired, response.meta.timings_ms)
        return response

    async def _assemble(self, parsed: LLMAnalysis, site: SiteContent, usage) -> LinkAnalysisResponse:
        brand = ground_brand(parsed.brand, site.pages)
        if not brand.name.strip():
            brand.name = site.home.og.get("og:site_name") or (site.home.title or site.url)

        grounded_services = [ground_service(s, site.pages) for s in parsed.top_services]
        grounded_audience = ground_target_audience(parsed.target_audience, site.pages)

        comp_dump = parsed.competitor_analysis.model_dump(exclude={"competitors"})
        verified = await self.competitors.verify(
            parsed.competitor_analysis.competitors,
            site.url,
            brand_name=brand.name,
            brand_category=parsed.brand.category or parsed.competitor_analysis.main_category,
            services=grounded_services,
            geographic_focus=grounded_audience.geographic_focus,
            target_audience=grounded_audience.primary_audience,
        )
        competitor_analysis = CompetitorAnalysis(**comp_dump, competitors=verified)
        eligible_comp_names = {
            c.name.strip().lower()
            for c in verified
            if c.name and c.relevance.status in ("likely_relevant", "possible")
        }
        eligible_comp_hosts = {
            (urlsplit(c.website).hostname or "").lower().removeprefix("www.")
            for c in verified
            if c.website and c.relevance.status in ("likely_relevant", "possible")
        }
        competitor_analysis.comparison = [
            c for c in competitor_analysis.comparison
            if c.role == "your_brand"
            or (
                c.brand_name.strip().lower() in eligible_comp_names
                or any(h and h in c.brand_name.strip().lower() for h in eligible_comp_hosts)
            )
        ]

        grounded_queries = ensure_platform_coverage(
            parsed.target_search_queries,
            brand_name=brand.name,
            brand_category=parsed.brand.category or parsed.competitor_analysis.main_category,
            geographic_focus=grounded_audience.geographic_focus,
        )

        current, checks = seo_audit(site.home)
        seo = SeoImprovements(
            **parsed.seo_improvements.model_dump(),
            recommended_title_length=len(parsed.seo_improvements.recommended_title),
            meta_description_length=len(parsed.seo_improvements.meta_description),
            current_page=current, on_page_checks=checks,
        )

        notes = [NO_VOLUME_NOTE]
        if len(verified) < 3:
            notes.append("Fewer than 3 competitors were returned/kept: the model was told to omit companies it was not "
                         "confident exist, and duplicates or the brand itself were removed.")
        if seo.recommended_title_length > 60:
            notes.append(f"Recommended title is {seo.recommended_title_length} chars (>60) and may be truncated in search results.")
        if not 70 <= seo.meta_description_length <= 160:
            notes.append(f"Recommended meta description is {seo.meta_description_length} chars (ideal 70-160).")

        downgraded_count = sum(
            1 for orig, g in zip(parsed.top_services, grounded_services)
            if orig.source == "website" and g.source == "inferred"
        )
        if downgraded_count > 0:
            notes.append(
                f"{downgraded_count} service(s) claimed as website facts lacked verifiable source quotations "
                "and were downgraded to 'inferred'."
            )
        elif any(s.source == "inferred" for s in grounded_services):
            notes.append("Services with source='inferred' were not explicitly listed on the analysed pages.")

        if grounded_audience.geographic_focus:
            if grounded_audience.geographic_focus_provenance == "website_verified":
                notes.append("Geographic focus is website-verified with source quotation evidence.")
            else:
                notes.append("Geographic focus is an AI inference; no verified website address quotation was matched.")

        notes.append(
            "Customer profiles, needs, challenges, and marketing segments are AI-generated inferences "
            "and recommendations, not website-verified facts."
        )
        notes.append(
            "Competitor identifications, descriptions, strengths, and weaknesses are AI analytical suggestions "
            "and market interpretations, not verified facts from the analysed website."
        )

        if any(p.truncated_source for p in site.pages):
            notes.append("A page was larger than the size cap; only its first part was analysed.")

        return LinkAnalysisResponse(
            website=site.url,
            brand=brand,
            analysis=Analysis(
                competitor_analysis=competitor_analysis,
                target_search_queries=grounded_queries,
                seo_improvements=seo,
                top_services=grounded_services,
                target_audience=grounded_audience,
            ),
            meta=Meta(
                provider=usage.provider, model=usage.model, pages_analyzed=[p.url for p in site.pages],
                content_chars_sent=len(site.prompt_text), input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens, cached_input_tokens=usage.cached_input_tokens,
                estimated_cost_usd=usage.cost_usd, llm_calls=usage.calls, repaired_output=usage.repaired,
                fallback_used=usage.fallback_used, notes=notes,
            ),
        )
