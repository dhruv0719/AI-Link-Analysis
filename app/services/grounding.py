"""Runtime evidence grounding and verification.

Validates LLM-supplied quotations against the actual fetched website pages
using deterministic exact matching with safe text normalization.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse, urlunparse

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


def canonicalize_url(u: str) -> str:
    """Canonicalize a URL for strict deterministic comparison without collisions.

    - Strips fragment (#...).
    - Standardizes scheme and host to lowercase.
    - Strips default ports (:80 for http, :443 for https).
    - Normalizes path: redundant slashes removed, trailing slash stripped (unless root '/').
    - Canonicalizes query string with sorted key-value pairs (preserving parameters).
    - Preserves domain and www prefix (does NOT strip www. or drop query strings).
    """
    if not u or not u.strip():
        return ""
    clean_u, _ = urldefrag(u.strip())
    parsed = urlparse(clean_u)
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()

    if ":" in netloc:
        host, port = netloc.split(":", 1)
        if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
            netloc = host

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    query = ""
    if parsed.query:
        params = sorted(parse_qsl(parsed.query, keep_blank_values=True))
        query = urlencode(params)

    return urlunparse((scheme, netloc, path, "", query, ""))


def find_matching_page(source_url: str, pages: list[PageContent]) -> PageContent | None:
    """Find the fetched page matching source_url using collision-safe multi-tier matching.

    Guarantees:
    - Never collides between two distinct fetched pages (e.g. example.com vs www.example.com,
      or /products?category=shoes vs /products?category=hats).
    - Resolves relative URLs against the base page (pages[0].url).
    - Matches recorded redirect aliases (from PageContent.aliases) unambiguously.
    - Allows unambiguous www-prefix mismatch ONLY when exact parameters and path match
      and exactly one page in the fetched pages set matches (fails closed if ambiguous).
    - Ignores client-side fragments (#...) deterministically.
    """
    if not source_url or not pages:
        return None

    base_url = pages[0].url if pages and pages[0].url else ""
    full_target = urljoin(base_url, source_url.strip()) if base_url else source_url.strip()
    target_canon = canonicalize_url(full_target)
    if not target_canon:
        return None

    # Tier 1: Exact canonical match against page.url
    for p in pages:
        if target_canon == canonicalize_url(p.url):
            return p

    # Tier 2: Match against recorded redirect / canonical aliases of pages
    alias_matches: list[PageContent] = []
    for p in pages:
        p_aliases = getattr(p, "aliases", []) or []
        for alias in p_aliases:
            if target_canon == canonicalize_url(alias):
                if p not in alias_matches:
                    alias_matches.append(p)
                break
    if len(alias_matches) == 1:
        return alias_matches[0]
    elif len(alias_matches) > 1:
        # Ambiguous alias collision - refuse to guess
        return None

    # Parse target components for relaxed unambiguous fallback
    target_parsed = urlparse(target_canon)
    target_host = target_parsed.hostname or ""
    target_path = target_parsed.path or "/"
    target_query = target_parsed.query

    # Tier 3: Unambiguous www-prefix mismatch (only if path and query match EXACTLY)
    # Allows matching when the LLM outputs non-www apex but page is www (or vice versa),
    # provided there is NO ambiguity and NO collision.
    target_host_apex = target_host.removeprefix("www.")
    www_matches: list[PageContent] = []
    for p in pages:
        p_canon = canonicalize_url(p.url)
        p_parsed = urlparse(p_canon)
        p_host = p_parsed.hostname or ""
        p_host_apex = p_host.removeprefix("www.")
        p_path = p_parsed.path or "/"
        p_query = p_parsed.query

        if (
            target_host_apex == p_host_apex
            and target_path == p_path
            and target_query == p_query
        ):
            www_matches.append(p)

    if len(www_matches) == 1:
        return www_matches[0]

    return None


def match_quote_in_corpus(norm_quote: str, norm_corpus: str) -> bool:
    """Check if normalized quote appears in normalized corpus."""
    if not norm_quote or not norm_corpus:
        return False
    if norm_quote in norm_corpus:
        return True
    stripped_quote = norm_quote.rstrip(". , ; : ! ? -")
    if len(stripped_quote) >= 6 and stripped_quote in norm_corpus:
        return True
    return False


def verify_and_canonicalize_evidence(
    ev: Evidence, pages: list[PageContent]
) -> Evidence | None:
    """Verify that an Evidence quote exists in the corresponding fetched page and return
    a canonicalized Evidence object pointing to the actual fetched page URL.

    Returns None if the evidence is invalid, fabricated, or misattributed.
    """
    if not ev or not ev.quote or not ev.source_url:
        return None

    norm_quote = normalize_text_for_matching(ev.quote)
    cleaned_quote = norm_quote.strip(_STRIP_PUNCT)
    if len(cleaned_quote) < 6:
        return None

    matched_page = find_matching_page(ev.source_url, pages)
    if not matched_page:
        return None

    corpus = normalize_text_for_matching(page_search_corpus(matched_page))
    if match_quote_in_corpus(norm_quote, corpus) or match_quote_in_corpus(cleaned_quote, corpus):
        # Update source_url to the actual fetched page URL, not an alias, relative path, or fragment
        return ev.model_copy(update={"source_url": matched_page.url})

    return None


def validate_evidence(ev: Evidence, pages: list[PageContent]) -> bool:
    """Verify that an Evidence item's quote exists in the corresponding fetched page content."""
    return verify_and_canonicalize_evidence(ev, pages) is not None


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
    """Validate brand evidence and set brand name, summary, and overall provenance.

    Guarantees returned evidence objects point to the actual fetched page URL.
    """
    name_verified = is_brand_name_verified(brand.name, pages)
    name_prov: Provenance = "website_verified" if name_verified else "inferred"

    valid_ev: list[Evidence] = []
    for ev in brand.evidence:
        canonical_ev = verify_and_canonicalize_evidence(ev, pages)
        if canonical_ev and not is_generic_boilerplate_quote(canonical_ev.quote, brand.name):
            valid_ev.append(canonical_ev)

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
    - Verified evidence objects point to the actual fetched page URL.
    """
    valid_ev = [
        canonical_ev
        for ev in service.evidence
        if (canonical_ev := verify_and_canonicalize_evidence(ev, pages)) is not None
    ]

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
    """Validate geographic focus evidence and establish audience provenance.

    Guarantees returned evidence objects point to the actual fetched page URL.
    """
    geo = audience.geographic_focus
    if not geo or not geo.strip():
        return audience.model_copy(update={
            "geographic_focus": None,
            "geographic_focus_provenance": None,
            "geographic_focus_evidence": [],
            "audience_provenance": "inferred",
        })

    valid_geo_ev = [
        canonical_ev
        for ev in audience.geographic_focus_evidence
        if (canonical_ev := verify_and_canonicalize_evidence(ev, pages)) is not None
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
