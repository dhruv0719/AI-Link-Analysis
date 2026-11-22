"""Runtime evidence grounding and verification.

Validates LLM-supplied quotations against the actual fetched website pages
using deterministic exact matching with safe text normalization.
"""
from __future__ import annotations

import logging
import re
import unicodedata

from app.schemas import Brand, Evidence, Service, TargetAudience
from app.services.website_extractor import PageContent
from app.utils.validators import normalize_url

log = logging.getLogger("link_analysis.grounding")

# Punctuation to strip from ends of quotes
_STRIP_PUNCT = " '\"“”‘’`.,;:!?-–—"


def normalize_text_for_matching(text: str) -> str:
    """Normalize text for deterministic fuzzy-safe substring matching.

    - Applies Unicode NFKC normalization.
    - Normalizes curly/smart quotes and dashes to standard ASCII.
    - Collapses all whitespace, newlines, and tabs to a single space.
    - Strips leading Markdown heading markers (#) if present.
    - Lowercases for case-insensitive matching.
    """
    if not text:
        return ""
    # Unicode decomposition/composition
    text = unicodedata.normalize("NFKC", text)
    # Smart quotes & dashes
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    text = text.replace("—", "-").replace("–", "-")
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


def find_matching_page(source_url: str, pages: list[PageContent]) -> PageContent | None:
    """Find the fetched page matching source_url using normalized URL comparison."""
    if not source_url or not pages:
        return None
    try:
        norm_target = normalize_url(source_url).rstrip("/")
    except Exception:
        return None

    for p in pages:
        try:
            norm_p = normalize_url(p.url).rstrip("/")
            if norm_target == norm_p:
                return p
        except Exception:
            continue
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


def ground_service(service: Service, pages: list[PageContent]) -> Service:
    """Validate a service's evidence and establish its factual provenance.

    - If source='website' and has valid evidence: retained as 'website' with provenance='website_verified'.
    - If source='website' but lacks valid evidence: downgraded to 'inferred' with provenance='inferred'.
    - If source='inferred': retained as 'inferred' with provenance='inferred'.
    """
    valid_ev = [ev for ev in service.evidence if validate_evidence(ev, pages)]

    if service.source == "website":
        if valid_ev:
            return service.model_copy(update={
                "source": "website",
                "provenance": "website_verified",
                "evidence": valid_ev[:2],
            })
        else:
            log.info("downgrading service '%s' to inferred (no verified evidence)", service.name)
            return service.model_copy(update={
                "source": "inferred",
                "provenance": "inferred",
                "evidence": [],
            })
    else:
        return service.model_copy(update={
            "source": "inferred",
            "provenance": "inferred",
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


def ground_brand(brand: Brand, pages: list[PageContent]) -> Brand:
    """Validate brand evidence and set brand provenance."""
    valid_ev = [ev for ev in brand.evidence if validate_evidence(ev, pages)]
    if valid_ev:
        return brand.model_copy(update={
            "provenance": "website_verified",
            "evidence": valid_ev[:2],
        })
    else:
        return brand.model_copy(update={
            "provenance": "inferred",
            "evidence": [],
        })
