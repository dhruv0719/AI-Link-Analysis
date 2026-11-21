"""Competitor post-processing.

The LLM *suggests* competitors from its own knowledge. We do not pretend that is research. Instead
each suggestion is sanity-checked against reality: is the suggested website live, and does the page
actually mention the competitor's name? Results are labelled in `verification`, and the competitor
is always marked `source: "ai_suggested"`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlsplit

from app.config import Settings
from app.errors import AppError
from app.schemas import Competitor, LLMCompetitor
from app.services.website_extractor import SafeFetcher, parse_page
from app.utils.validators import normalize_url

log = logging.getLogger("link_analysis.competitors")
_TOKEN = re.compile(r"[a-z0-9]{3,}")
_GENERIC = {"inc", "ltd", "llc", "corp", "the", "and", "company", "group", "technologies", "solutions", "services", "software"}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _name_tokens(name: str) -> set[str]:
    return {t for t in _TOKEN.findall(name.lower()) if t not in _GENERIC}


class CompetitorService:
    def __init__(self, fetcher: SafeFetcher, settings: Settings):
        self.fetcher = fetcher
        self.settings = settings

    async def verify(self, suggestions: list[LLMCompetitor], brand_url: str) -> list[Competitor]:
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
                verification, title = await self._check_one(comp)
            return Competitor(**comp.model_dump(), verification=verification, verified_page_title=title)

        return list(await asyncio.gather(*(check(c) for c in unique)))

    async def _check_one(self, comp: LLMCompetitor) -> tuple[str, str | None]:
        if not comp.website:
            return "no_website_provided", None
        try:
            url = normalize_url(comp.website, allow_private=self.settings.allow_private_urls)
        except AppError:
            return "invalid_website_url", None
        try:
            fetched = await self.fetcher.fetch(url, max_bytes=250_000, timeout=6)
        except AppError as exc:
            # A site that blocks bots (403/429) still *exists*; treat only hard failures as unreachable.
            if exc.code == "website_access_denied":
                return "live_site_name_not_matched", None
            log.info("competitor %s unreachable: %s", comp.name, exc.code)
            return "unreachable", None
        except Exception:  # pragma: no cover - defensive
            return "unreachable", None

        page = parse_page(fetched.body, fetched.url)
        # The domain itself is NOT evidence (the LLM supplied it); the page content must mention the name.
        haystack = " ".join(filter(None, [page.title, page.og.get("og:site_name"), page.meta_description, " ".join(page.h1)])).lower()
        tokens = _name_tokens(comp.name)
        matched = bool(tokens) and any(t in haystack for t in tokens)
        return ("verified_live_site" if matched else "live_site_name_not_matched"), page.title
