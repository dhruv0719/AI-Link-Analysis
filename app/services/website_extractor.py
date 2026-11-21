"""Fetch a website safely and turn its HTML into compact, LLM-ready text.

Pipeline: SafeFetcher (SSRF-checked, manual redirects, size cap) -> parse_page (clean HTML,
metadata, headings, JSON-LD, links) -> WebsiteExtractor (homepage + a few key internal pages ->
one budgeted text block + SEO facts).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

from app.config import Settings
from app.errors import AppError
from app.schemas import CurrentPageSeo, SeoCheck
from app.utils.validators import default_port, normalize_url, resolve_public_ips

log = logging.getLogger("link_analysis.extractor")

HTML_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
# NOTE: <form> is deliberately NOT here - ASP.NET-style sites wrap the whole page in one <form>.
NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe", "canvas", "template", "button",
              "select", "option", "dialog", "object", "embed", "video", "audio", "picture", "map"]
HINT_EXEMPT_TAGS = {"html", "body", "main", "article", "form"}  # a stray "cookie-open" class must not wipe the page
NOISE_HINTS = re.compile(r"(cookie|consent|gdpr|popup|modal|newsletter-signup|subscribe-banner|chat-widget|breadcrumb)", re.I)
BLOCK_TAGS = ["h1", "h2", "h3", "h4", "p", "li", "td", "th", "blockquote", "figcaption", "dt", "dd", "summary"]
PAGE_HINTS = [  # (regex on path+anchor text, score)
    (re.compile(r"(service|solution|what-we-do|offering|capabilit|product|feature|platform)", re.I), 3),
    (re.compile(r"(about|company|who-we-are|our-story|team)", re.I), 3),
    (re.compile(r"(pricing|plans|packages)", re.I), 2),
    (re.compile(r"(industr|portfolio|case-stud|customers|clients|work)", re.I), 1),
]
SKIP_PATH = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|mp4|docx?|xlsx?)$|/(login|signin|sign-in|signup|cart|checkout|account|privacy|terms|cookie|wp-admin|tag|category|author)(/|$)", re.I)
SOCIAL_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com", "tiktok.com")


# ============================================================================ fetching
@dataclass
class FetchedPage:
    url: str          # final URL after redirects
    status: int
    body: bytes
    content_type: str
    elapsed_ms: int
    truncated: bool = False


class SafeFetcher:
    """HTTP GET with SSRF validation on every hop. Never lets httpx follow redirects itself."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings

    async def fetch(self, url: str, *, max_bytes: int | None = None, timeout: float | None = None) -> FetchedPage:
        s = self.settings
        max_bytes = max_bytes or s.fetch_max_bytes
        timeout = timeout or s.fetch_timeout_seconds
        started = time.perf_counter()
        current = normalize_url(url, allow_private=s.allow_private_urls)

        for _hop in range(s.max_redirects + 1):
            parts = urlsplit(current)
            await resolve_public_ips(parts.hostname or "", default_port(current), allow_private=s.allow_private_urls)
            try:
                async with self.client.stream(
                    "GET", current, follow_redirects=False, timeout=timeout,
                    headers={"User-Agent": s.user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                             "Accept-Language": "en;q=0.9,*;q=0.5"},
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise AppError(502, "website_error", "The website sent a redirect without a target.")
                        # Re-validate the next hop (scheme, host, resolved IPs) on the next loop turn.
                        current = normalize_url(urljoin(current, location), allow_private=s.allow_private_urls)
                        continue
                    self._raise_for_status(resp.status_code)
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith(HTML_TYPES):
                        raise AppError(415, "unsupported_content_type",
                                       f"The URL returned '{ctype}', not an HTML page.")
                    chunks, size, truncated = [], 0, False
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        chunks.append(chunk)
                        if size >= max_bytes:
                            truncated = True
                            break
                    return FetchedPage(current, resp.status_code, b"".join(chunks)[:max_bytes], ctype,
                                       int((time.perf_counter() - started) * 1000), truncated)
            except AppError:
                raise
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout, httpx.WriteTimeout):
                raise AppError(504, "website_timeout", f"The website did not respond within {timeout:.0f}s.") from None
            except httpx.ConnectError:
                raise AppError(502, "website_unreachable",
                               "Could not connect to the website (server down, refused the connection, or TLS error).") from None
            except httpx.HTTPError as exc:
                raise AppError(502, "website_error", f"Failed to fetch the website ({type(exc).__name__}).") from None
        raise AppError(502, "too_many_redirects", f"The website redirected more than {s.max_redirects} times.")

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if status < 400:
            return
        if status in (404, 410):
            raise AppError(422, "page_not_found", f"The page was not found (HTTP {status}).")
        if status in (401, 403, 429, 451):
            raise AppError(502, "website_access_denied",
                           f"The website refused automated access (HTTP {status}). It may use bot protection or require login.")
        raise AppError(502, "website_error", f"The website returned an error (HTTP {status}).")


# ============================================================================ parsing
@dataclass
class PageContent:
    url: str
    title: str | None = None
    meta_description: str | None = None
    meta_keywords: str | None = None
    canonical: str | None = None
    lang: str | None = None
    robots: str | None = None
    has_viewport: bool = False
    og: dict[str, str] = field(default_factory=dict)
    h1: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)   # h2/h3 in order
    nav_labels: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)     # cleaned, de-duplicated text blocks
    jsonld: list[dict] = field(default_factory=list)
    images_total: int = 0
    images_missing_alt: int = 0
    internal_links: list[tuple[str, str]] = field(default_factory=list)  # (anchor text, absolute url)
    social_links: list[str] = field(default_factory=list)
    truncated_source: bool = False

    @property
    def word_count(self) -> int:
        return sum(len(b.split()) for b in self.blocks)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _meta(soup: BeautifulSoup, **attrs) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    content = tag.get("content") if tag else None
    return _clean(content) if content else None


def _jsonld(soup: BeautifulSoup) -> list[dict]:
    keep = {"Organization", "LocalBusiness", "Corporation", "WebSite", "Product", "Service", "ProfessionalService",
            "Store", "Restaurant", "SoftwareApplication", "EducationalOrganization", "MedicalBusiness"}
    out: list[dict] = []

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            if "@graph" in node:
                visit(node["@graph"])
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if any(t in keep for t in types if isinstance(t, str)):
                slim = {}
                for key in ("@type", "name", "description", "url", "serviceType", "areaServed", "slogan", "category",
                            "knowsAbout", "sameAs", "address", "offers", "makesOffer", "applicationCategory"):
                    if key in node:
                        slim[key] = node[key]
                if slim:
                    out.append(slim)

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            visit(json.loads(script.string or script.get_text() or ""))
        except (ValueError, TypeError):
            continue
    return out[:6]


def parse_page(body: bytes, url: str, *, truncated: bool = False) -> PageContent:
    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:  # pragma: no cover - lxml missing/odd markup
        soup = BeautifulSoup(body, "html.parser")

    page = PageContent(url=url, truncated_source=truncated)
    page.title = _clean(soup.title.get_text()) if soup.title and soup.title.get_text() else None
    page.meta_description = _meta(soup, name=re.compile(r"^description$", re.I))
    page.meta_keywords = _meta(soup, name=re.compile(r"^keywords$", re.I))
    page.robots = _meta(soup, name=re.compile(r"^robots$", re.I))
    page.has_viewport = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}) is not None
    canon = soup.find("link", rel=lambda v: v and "canonical" in (v if isinstance(v, list) else [v]))
    page.canonical = canon.get("href") if canon and canon.get("href") else None
    html_tag = soup.find("html")
    page.lang = html_tag.get("lang") if html_tag else None
    for key in ("og:site_name", "og:title", "og:description", "og:type"):
        value = _meta(soup, property=key)
        if value:
            page.og[key] = value
    page.jsonld = _jsonld(soup)

    imgs = soup.find_all("img")
    page.images_total = len(imgs)
    page.images_missing_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip())

    base = urlsplit(url)
    base_host = (base.hostname or "").removeprefix("www.")
    seen_links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        host = parts.hostname.removeprefix("www.")
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
        if host == base_host:
            if clean_url not in seen_links:
                seen_links.add(clean_url)
                page.internal_links.append((_clean(a.get_text())[:80], clean_url))
        elif any(host.endswith(s) for s in SOCIAL_HOSTS) and absolute not in page.social_links:
            page.social_links.append(absolute)

    # Navigation labels (kept separately: they reveal the offering structure) ...
    labels: list[str] = []
    for nav in soup.find_all("nav")[:3]:
        for a in nav.find_all("a")[:25]:
            label = _clean(a.get_text())
            if 2 <= len(label) <= 40 and label not in labels:
                labels.append(label)
    page.nav_labels = labels[:25]

    # ... then strip everything that is not primary content.
    for tag in soup(NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(["nav", "footer", "aside"]):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if getattr(tag, "decomposed", False) or not isinstance(tag, Tag) or tag.name in HINT_EXEMPT_TAGS:
            continue
        hint = " ".join([tag.get("id") or "", " ".join(tag.get("class") or [])])
        if (hint and NOISE_HINTS.search(hint)) or tag.get("hidden") is not None or tag.get("aria-hidden") == "true":
            tag.decompose()

    root = soup.find("main") or soup.find("article") or soup.body or soup
    page.h1 = [_clean(h.get_text()) for h in root.find_all("h1") if _clean(h.get_text())][:5]
    page.headings = [_clean(h.get_text()) for h in root.find_all(["h2", "h3"]) if _clean(h.get_text())][:40]

    seen: set[str] = set()
    for el in root.find_all(BLOCK_TAGS):
        text = _clean(el.get_text(" "))
        if not text:
            continue
        is_heading = el.name in ("h1", "h2", "h3", "h4")
        if not is_heading and len(text.split()) < 4:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        page.blocks.append(f"# {text}" if is_heading else text)
    return page


# ============================================================================ site-level
@dataclass
class SiteContent:
    url: str                       # final homepage URL
    pages: list[PageContent]
    prompt_text: str
    fetch_ms: int = 0

    @property
    def home(self) -> PageContent:
        return self.pages[0]

    @property
    def word_count(self) -> int:
        return sum(p.word_count for p in self.pages)

    @property
    def plain_text(self) -> str:
        """Lower-cased corpus used for groundedness checks."""
        return " ".join(" ".join(p.blocks + p.headings + [p.title or ""]) for p in self.pages).lower()


def _compact_jsonld(items: list[dict], limit: int = 1500) -> str:
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))[:limit] if items else ""


def pick_key_pages(home: PageContent, limit: int) -> list[str]:
    scored: dict[str, int] = {}
    home_path = urlsplit(home.url).path.rstrip("/") or "/"
    for text, link in home.internal_links:
        parts = urlsplit(link)
        path = parts.path.rstrip("/") or "/"
        if path == home_path or SKIP_PATH.search(parts.path):
            continue
        if path.count("/") > 2:      # deep pages (blog posts, product variants) are low value
            continue
        score = max((s for rx, s in PAGE_HINTS if rx.search(path) or rx.search(text)), default=0)
        if score:
            scored[link] = max(scored.get(link, 0), score)
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0])))
    picked, seen_paths = [], set()
    for link, _score in ranked:
        path = urlsplit(link).path.rstrip("/")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        picked.append(link)
        if len(picked) >= limit:
            break
    return picked


def build_prompt_text(pages: list[PageContent], budget: int) -> str:
    """Render pages into one text block, homepage first, within a character budget."""
    home = pages[0]
    header: list[str] = [f"URL: {home.url}"]
    if home.title:
        header.append(f"Page title: {home.title}")
    if home.meta_description:
        header.append(f"Meta description: {home.meta_description}")
    if home.meta_keywords:
        header.append(f"Meta keywords: {home.meta_keywords[:200]}")
    if home.og:
        header.append("OpenGraph: " + "; ".join(f"{k}={v[:160]}" for k, v in home.og.items()))
    if home.lang:
        header.append(f"Language: {home.lang}")
    if home.nav_labels:
        header.append("Navigation: " + " | ".join(home.nav_labels))
    if home.h1:
        header.append("H1: " + " | ".join(home.h1))
    ld = _compact_jsonld(home.jsonld)
    if ld:
        header.append(f"Structured data (JSON-LD): {ld}")
    if home.social_links:
        header.append("Social profiles: " + ", ".join(home.social_links[:6]))
    head_text = "\n".join(header)

    remaining = max(budget - len(head_text), 2000)
    shares = [0.55] + [0.45 / max(len(pages) - 1, 1)] * (len(pages) - 1)
    sections = [head_text]
    for page, share in zip(pages, shares):
        allowance = int(remaining * share)
        used, lines = 0, []
        for block in page.blocks:
            if used + len(block) + 1 > allowance:
                if allowance - used > 120:                 # squeeze in a clipped tail
                    lines.append(block[: allowance - used - 1] + "…")
                break
            lines.append(block)
            used += len(block) + 1
        label = "HOMEPAGE" if page is home else f"PAGE {page.url}" + (f" ({page.title})" if page.title else "")
        sections.append(f"--- {label} ---\n" + "\n".join(lines))
    return "\n\n".join(sections)


def seo_audit(page: PageContent) -> tuple[CurrentPageSeo, list[SeoCheck]]:
    """Deterministic on-page checks (no LLM, no invented data)."""
    checks: list[SeoCheck] = []

    def add(name: str, ok: bool | None, detail: str, warn_only: bool = False):
        status = "pass" if ok else ("warn" if warn_only or ok is None else "fail")
        checks.append(SeoCheck(check=name, status=status, detail=detail))

    t = page.title or ""
    add("Title tag", bool(t) and 30 <= len(t) <= 60,
        f"{len(t)} chars (ideal 30-60)" if t else "missing", warn_only=bool(t))
    d = page.meta_description or ""
    add("Meta description", bool(d) and 70 <= len(d) <= 160,
        f"{len(d)} chars (ideal 70-160)" if d else "missing", warn_only=bool(d))
    add("Single H1", len(page.h1) == 1, f"{len(page.h1)} H1 element(s) found", warn_only=len(page.h1) > 1)
    add("Canonical link", bool(page.canonical), page.canonical or "missing", warn_only=True)
    add("Mobile viewport meta", page.has_viewport, "present" if page.has_viewport else "missing")
    add("HTML lang attribute", bool(page.lang), page.lang or "missing", warn_only=True)
    add("OpenGraph tags", bool(page.og), ", ".join(page.og) if page.og else "missing", warn_only=True)
    add("Structured data (JSON-LD)", bool(page.jsonld), f"{len(page.jsonld)} relevant block(s)" if page.jsonld else "none found", warn_only=True)
    if page.images_total:
        missing = page.images_missing_alt
        add("Image alt text", missing == 0, f"{missing} of {page.images_total} images lack alt text", warn_only=missing <= page.images_total // 2)
    add("Indexable", "noindex" not in (page.robots or "").lower(), f"robots meta: {page.robots or 'not set'}")
    add("Content depth", page.word_count >= 300, f"{page.word_count} words of body text sampled", warn_only=True)

    return (
        CurrentPageSeo(title=page.title, meta_description=page.meta_description, h1=page.h1,
                       canonical=page.canonical, word_count_sampled=page.word_count),
        checks,
    )


class WebsiteExtractor:
    def __init__(self, fetcher: SafeFetcher, settings: Settings):
        self.fetcher = fetcher
        self.settings = settings

    async def extract_site(self, url: str) -> SiteContent:
        started = time.perf_counter()
        home_fetch = await self.fetcher.fetch(url)
        home = parse_page(home_fetch.body, home_fetch.url, truncated=home_fetch.truncated)
        pages = [home]

        extra_urls = pick_key_pages(home, self.settings.max_extra_pages) if self.settings.max_extra_pages > 0 else []
        if extra_urls:
            results = await asyncio.gather(
                *(self.fetcher.fetch(u, max_bytes=1_200_000, timeout=8) for u in extra_urls), return_exceptions=True)
            for u, res in zip(extra_urls, results):
                if isinstance(res, FetchedPage):
                    page = parse_page(res.body, res.url, truncated=res.truncated)
                    if page.word_count >= 30:
                        pages.append(page)
                else:
                    log.info("skipping extra page %s: %s", u, getattr(res, "message", res))

        text = build_prompt_text(pages, self.settings.max_content_chars)
        return SiteContent(url=home.url, pages=pages, prompt_text=text,
                           fetch_ms=int((time.perf_counter() - started) * 1000))
