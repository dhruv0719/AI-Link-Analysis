"""Shared fixtures: fake fetcher, fake LLM provider, service factory."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.errors import AppError
from app.services.analysis_service import AnalysisService
from app.services.competitor_service import CompetitorService
from app.services.llm_service import BaseProvider, Completion, LLMService
from app.services.website_extractor import FetchedPage, SafeFetcher, WebsiteExtractor
from app.utils.cache import TTLCache

FIXTURES = Path(__file__).parent / "fixtures"
HOME_HTML = (FIXTURES / "sample_home.html").read_bytes()
LLM_OUTPUT = json.loads((FIXTURES / "sample_llm_output.json").read_text())


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


def page(html: bytes | str, url: str) -> FetchedPage:
    body = html.encode() if isinstance(html, str) else html
    return FetchedPage(url=url, status=200, body=body, content_type="text/html", elapsed_ms=5)


def simple_site(title: str) -> bytes:
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1><p>{title} official website for customers around the world.</p></body></html>".encode()


class FakeFetcher(SafeFetcher):
    """Serves pages from a dict; anything missing raises 'unreachable'. Records requested URLs."""

    def __init__(self, pages: dict[str, FetchedPage | AppError], settings: Settings):
        super().__init__(client=None, settings=settings)  # type: ignore[arg-type]
        self.pages = pages
        self.requested: list[str] = []

    async def fetch(self, url, *, max_bytes=None, timeout=None):
        key = url if url in self.pages else url.rstrip("/") + "/"
        self.requested.append(url)
        result = self.pages.get(key)
        if result is None:
            raise AppError(502, "website_unreachable", "no route to host")
        if isinstance(result, AppError):
            raise result
        return result


class FakeProvider(BaseProvider):
    """Scripted provider: each item is a str (reply text) or an Exception to raise."""
    name = "openai"

    def __init__(self, script: list, name: str = "openai", model: str = "gpt-6-luna"):
        self.name, self.model = name, model
        self.script = list(script)
        self.prompts: list[str] = []

    async def complete(self, system, user, schema):
        self.prompts.append(user)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return Completion(item, input_tokens=6500, cached_input_tokens=0, output_tokens=3200, latency_ms=1200)


def make_settings(**overrides) -> Settings:
    base = dict(_env_file=None, environment="test", openai_api_key="sk-test", rate_limit_per_minute=1000, max_extra_pages=0)
    base.update(overrides)
    return Settings(**base)


def make_service(settings: Settings, pages: dict, providers: list[FakeProvider]) -> tuple[AnalysisService, FakeFetcher, LLMService]:
    fetcher = FakeFetcher(pages, settings)
    llm = LLMService(settings, client=None)  # type: ignore[arg-type]
    llm.chain = providers
    llm.RETRY_DELAYS = (0.0, 0.0)
    service = AnalysisService(settings, WebsiteExtractor(fetcher, settings), llm,
                              CompetitorService(fetcher, settings), TTLCache(3600, 10))
    return service, fetcher, llm


def default_pages() -> dict:
    return {
        "https://acme.example/": page(HOME_HTML, "https://acme.example/"),
        "https://www.wix.com/": page(simple_site("Wix.com: Create a Website"), "https://www.wix.com/"),
        "https://www.squarespace.com/": page(simple_site("Squarespace - Website Builder"), "https://www.squarespace.com/"),
        "https://www.shopify.com/": page(simple_site("Some unrelated parked domain page"), "https://www.shopify.com/"),
        # webflow.com intentionally missing -> unreachable
    }
