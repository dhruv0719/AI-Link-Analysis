"""Runtime evidence grounding and verification.

Validates LLM-supplied quotations against the actual fetched website pages
using deterministic exact matching with safe text normalization.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import urldefrag, urljoin, urlparse

from app.schemas import Brand, Evidence, Provenance, Service, TargetAudience
from app.services.website_extractor import PageContent

log = logging.getLogger("link_analysis.grounding")

# Punctuation to strip from ends of quotes
_STRIP_PUNCT = " '\"“”‘’`.,;:!?-–—"

_GENERIC_BOILERPLATE_PATTERNS = [
    re.compile(
        r"\b(all rights reserved|privacy policy|terms of service|terms of use|"
        r"terms and conditions|cookie policy|cookie settings|cookies policy|copyright \d{4})\b",
        re.I,
    ),
    re.compile(
        r"\b(home\s+about\s+services\s+contact|navigation|quick links|follow us on|site map|sitemap)\b",
        re.I,
    ),
]


def normalize_text_for_matching(text: str) -> str:
    """Normalize text for deterministic fuzzy-safe substring matching.

    - Applies Unicode NFKC normalization.
    - Strips zero-width characters.
    - Normalizes curly/smart quotes and dashes to standard ASCII.
    - Normalizes bracket/punctuation spacing artifacts from HTML concatenation (e.g. ')to' -> ') to').
    - Strips Markdown heading markers (#) so quotes from headings match body/heading text.
    - Collapses all whitespace, newlines, and tabs to a single space.
    - Lowercases for case-insensitive matching.
    """
    if not text:
        return ""
    # Unicode decomposition/composition
    text = unicodedata.normalize("NFKC", text)
    # Zero-width spaces/joiners
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    # Smart quotes & dashes
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'").replace("`", "'")
    text = text.replace("—", "-").replace("–", "-")
    # Punctuation spacing artifacts commonly found in HTML extraction
    text = re.sub(r"([A-Za-z0-9])([(\[{])", r"\1 \2", text)
    text = re.sub(r"([)\]}.,;:!?])([A-Za-z])", r"\1 \2", text)
    text = re.sub(r"([)\]}])([0-9])", r"\1 \2", text)
    # Strip markdown heading markers
    text = re.sub(r"#+\s*", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.lower().strip()


def page_search_corpus(page: PageContent) -> str:
    """Build a consolidated text corpus for a single fetched page."""
    elements: list[str] = []
    if page.title:
        elements.append(page.title)
    if page.meta_description:
        elements.append(page.meta_description)
    if page.meta_keywords:
        elements.append(page.meta_keywords)
    if page.h1:
        elements.extend(page.h1)
    if page.headings:
        elements.extend(page.headings)
    if page.blocks:
        # Strip heading markers like '# ' from blocks for clean text matching
        for b in page.blocks:
            elements.append(re.sub(r"^#+\s*", "", b))
    if page.footer_text:
        elements.append(page.footer_text)
    if page.nav_labels:
        elements.extend(page.nav_labels)
    for v in page.og.values():
        elements.append(v)
    return " ".join(elements)


def _canonical_url_tuple(u: str) -> tuple[str, str]:
    """Return (hostname_without_www, normalized_path) for URL matching."""
    clean_u, _ = urldefrag(u.strip())
    parsed = urlparse(clean_u)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return (host, path)


def find_matching_page(source_url: str, pages: list[PageContent]) -> PageContent | None:
    """Find the fetched page matching source_url using robust URL comparison.

    Handles:
    - Trailing slashes
    - Relative URLs (resolving against pages[0].url if available)
    - www. vs non-www. hostname differences
    - URL fragments / anchors
    - Case insensitivity
    """
    if not source_url or not pages:
        return None

    base_url = pages[0].url if pages and pages[0].url else ""
    full_target = urljoin(base_url, source_url.strip()) if base_url else source_url.strip()

    target_host, target_path = _canonical_url_tuple(full_target)
    if not target_host and not target_path:
        return None

    # 1. Exact canonical (host, path) match
    for p in pages:
        p_host, p_path = _canonical_url_tuple(p.url)
        if target_host == p_host and target_path == p_path:
            return p

    # 2. Root page fallback if target has root path and host matches
    if target_path == "/":
        for p in pages:
            p_host, p_path = _canonical_url_tuple(p.url)
            if target_host == p_host and p_path == "/":
                return p

    # 3. Path-only match if host was empty or matches base host
    base_host, _ = _canonical_url_tuple(base_url)
    if not target_host or target_host == base_host:
        for p in pages:
            _, p_path = _canonical_url_tuple(p.url)
            if target_path == p_path:
                return p

    return None


def match_quote_in_corpus(norm_quote: str, norm_corpus: str) -> bool:
    """Check if normalized quote appears in normalized corpus."""
    if not norm_quote or not norm_corpus:
        return False
    if norm_quote in norm_corpus:
        return True
    # Strip trailing punctuation (. , ; : ! ? -) and check again
    stripped_quote = norm_quote.rstrip(". , ; : ! ? -")
    if len(stripped_quote) >= 6 and stripped_quote in norm_corpus:
        return True
    return False


def validate_evidence(ev: Evidence, pages: list[PageContent]) -> bool:
    """Verify that an Evidence item's quote exists in the corresponding fetched page content."""
    if not ev or not ev.quote or not ev.source_url:
        return False

    norm_quote = normalize_text_for_matching(ev.quote)
    cleaned_quote = norm_quote.strip(_STRIP_PUNCT)
    if len(cleaned_quote) < 6:
        return False

    matched_page = find_matching_page(ev.source_url, pages)
    if not matched_page:
        return False

    corpus = normalize_text_for_matching(page_search_corpus(matched_page))
    return match_quote_in_corpus(norm_quote, corpus) or match_quote_in_corpus(cleaned_quote, corpus)


def is_generic_boilerplate_quote(quote: str, brand_name: str | None = None) -> bool:
    """Check if quote is generic legal/navigational boilerplate lacking brand substance.

    If the quote contains generic boilerplate (e.g. 'All rights reserved. Privacy Policy')
    and does NOT contain the brand name, it cannot be used to verify brand identity.
    """
    if not quote:
        return True
    norm_quote = normalize_text_for_matching(quote)

    has_boilerplate = any(p.search(norm_quote) for p in _GENERIC_BOILERPLATE_PATTERNS)
    if not has_boilerplate:
        return False

    if brand_name:
        norm_brand = normalize_text_for_matching(brand_name)
        if len(norm_brand) >= 2 and norm_brand in norm_quote:
            return False

    return True


def is_brand_name_verified(brand_name: str, pages: list[PageContent]) -> bool:
    """Verify whether the brand name is grounded in the website pages (title, OG, or corpus)."""
    if not brand_name or not pages:
        return False
    norm_name = normalize_text_for_matching(brand_name)
    if len(norm_name) < 2:
        return False

    for p in pages:
        og_site = normalize_text_for_matching(p.og.get("og:site_name", ""))
        if norm_name in og_site or (og_site and og_site in norm_name):
            return True
        og_title = normalize_text_for_matching(p.og.get("og:title", ""))
        if norm_name in og_title:
            return True

        title = normalize_text_for_matching(p.title)
        if norm_name in title:
            return True

        corpus = normalize_text_for_matching(page_search_corpus(p))
        if norm_name in corpus:
            return True

    return False


def ground_brand(brand: Brand, pages: list[PageContent]) -> Brand:
    """Validate brand evidence and set brand name, summary, and overall provenance."""
    name_verified = is_brand_name_verified(brand.name, pages)
    name_prov: Provenance = "website_verified" if name_verified else "inferred"

    valid_ev = [
        ev for ev in brand.evidence
        if validate_evidence(ev, pages) and not is_generic_boilerplate_quote(ev.quote, brand.name)
    ]

    summary_prov: Provenance = "website_verified" if valid_ev else "inferred"
    overall_prov: Provenance = "website_verified" if (name_verified and valid_ev) else "inferred"

    return brand.model_copy(update={
        "provenance": overall_prov,
        "name_provenance": name_prov,
        "summary_provenance": summary_prov,
        "evidence": valid_ev[:2],
    })


def ground_service(service: Service, pages: list[PageContent]) -> Service:
    """Validate a service's evidence and establish its factual provenance and description provenance.

    - If source='website' and has valid evidence: retained as 'website' with provenance='website_verified'.
    - If source='website' but lacks valid evidence: downgraded to 'inferred' with provenance='inferred'.
    - If source='inferred': retained as 'inferred' with provenance='inferred'.
    - description_provenance: 'website_verified' if the description is directly grounded in website text,
      otherwise 'inferred'.
    """
    valid_ev = [ev for ev in service.evidence if validate_evidence(ev, pages)]

    desc_prov: Provenance = "inferred"
    if service.description and len(service.description.strip()) >= 10:
        norm_desc = normalize_text_for_matching(service.description).strip(_STRIP_PUNCT)
        if len(norm_desc) >= 10:
            for p in pages:
                corpus = normalize_text_for_matching(page_search_corpus(p))
                if match_quote_in_corpus(norm_desc, corpus):
                    desc_prov = "website_verified"
                    break

    if service.source == "website":
        if valid_ev:
            return service.model_copy(update={
                "source": "website",
                "provenance": "website_verified",
                "description_provenance": desc_prov,
                "evidence": valid_ev[:2],
            })
        else:
            log.info("downgrading service '%s' to inferred (no verified evidence)", service.name)
            return service.model_copy(update={
                "source": "inferred",
                "provenance": "inferred",
                "description_provenance": "inferred",
                "evidence": [],
            })
    else:
        return service.model_copy(update={
            "source": "inferred",
            "provenance": "inferred",
            "description_provenance": desc_prov if valid_ev else "inferred",
            "evidence": valid_ev[:2] if valid_ev else [],
        })


def ground_target_audience(
    audience: TargetAudience, pages: list[PageContent]
) -> TargetAudience:
    """Validate geographic focus evidence and establish audience provenance."""
    geo = audience.geographic_focus
    if not geo or not geo.strip():
        return audience.model_copy(update={
            "geographic_focus": None,
            "geographic_focus_provenance": None,
            "geographic_focus_evidence": [],
            "audience_provenance": "inferred",
        })

    valid_geo_ev = [
        ev for ev in audience.geographic_focus_evidence
        if validate_evidence(ev, pages)
    ]

    if valid_geo_ev:
        return audience.model_copy(update={
            "geographic_focus": geo.strip(),
            "geographic_focus_provenance": "website_verified",
            "geographic_focus_evidence": valid_geo_ev[:2],
            "audience_provenance": "inferred",
        })
    else:
        return audience.model_copy(update={
            "geographic_focus": geo.strip(),
            "geographic_focus_provenance": "inferred",
            "geographic_focus_evidence": [],
            "audience_provenance": "inferred",
        })
