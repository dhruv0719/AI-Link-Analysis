"""Orchestrates the whole flow: validate -> cache -> extract -> LLM -> verify/enrich -> response."""
from __future__ import annotations

import logging
import time

from app.config import Settings
from app.errors import AppError
from app.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from app.schemas import (
    Analysis, CompetitorAnalysis, LinkAnalysisResponse, LLMAnalysis, Meta, SeoImprovements,
)
from app.services.competitor_service import CompetitorService
from app.services.grounding import ground_brand, ground_service, ground_target_audience
from app.services.llm_service import LLMService
from app.services.website_extractor import SiteContent, WebsiteExtractor, seo_audit
from app.utils.cache import TTLCache
from app.utils.validators import normalize_url

log = logging.getLogger("link_analysis.service")
MIN_WORDS = 40
NO_VOLUME_NOTE = "No search-volume, ranking or traffic numbers are included: none were sourced from an API."


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

        comp_dump = parsed.competitor_analysis.model_dump(exclude={"competitors"})
        verified = await self.competitors.verify(parsed.competitor_analysis.competitors, site.url)
        competitor_analysis = CompetitorAnalysis(**comp_dump, competitors=verified)

        current, checks = seo_audit(site.home)
        seo = SeoImprovements(
            **parsed.seo_improvements.model_dump(),
            recommended_title_length=len(parsed.seo_improvements.recommended_title),
            meta_description_length=len(parsed.seo_improvements.meta_description),
            current_page=current, on_page_checks=checks,
        )

        grounded_services = [ground_service(s, site.pages) for s in parsed.top_services]
        grounded_audience = ground_target_audience(parsed.target_audience, site.pages)

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

        if any(p.truncated_source for p in site.pages):
            notes.append("A page was larger than the size cap; only its first part was analysed.")

        return LinkAnalysisResponse(
            website=site.url,
            brand=brand,
            analysis=Analysis(
                competitor_analysis=competitor_analysis,
                target_search_queries=parsed.target_search_queries,
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
