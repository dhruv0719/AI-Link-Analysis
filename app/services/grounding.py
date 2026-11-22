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
    - Preserves exact path structure (/pricing and /pricing/ remain distinct to avoid collisions).
    - Canonicalizes query string with sorted key-value pairs (preserving repeated keys).
    - Preserves domain and www prefix.
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

    query = ""
    if parsed.query:
        # parse_qsl preserves repeated keys; sorted sorts by (key, value) tuples
        params = sorted(parse_qsl(parsed.query, keep_blank_values=True))
        query = urlencode(params)

    return urlunparse((scheme, netloc, path, "", query, ""))


def find_matching_page(source_url: str, pages: list[PageContent]) -> PageContent | None:
    """Find the fetched page matching source_url using collision-safe multi-tier matching.

    Guarantees:
    - Never collides between two distinct fetched pages (e.g. example.com vs www.example.com,
      /pricing vs /pricing/, or /products?category=shoes vs /products?category=hats).
    - Resolves relative URLs against the base page (pages[0].url).
    - Matches recorded redirect aliases (from PageContent.aliases) unambiguously.
    - Allows unambiguous www-prefix or trailing-slash mismatch ONLY when exact parameters
      match and exactly one page in the fetched pages set matches (fails closed if ambiguous).
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

    # Parse target components for relaxed unambiguous fallbacks
    target_parsed = urlparse(target_canon)
    target_host = target_parsed.hostname or ""
    target_path = target_parsed.path or "/"
    target_query = target_parsed.query

    # Tier 3: Unambiguous www-prefix mismatch (only if path and query match EXACTLY)
    target_host_apex = target_host.removeprefix("www.")
    www_matches: list[PageContent] = []
    for p in pages:
        p_canon = canonicalize_url(p.url)
        p_parsed = urlparse(p_canon)
        p_host = (p_parsed.hostname or "").removeprefix("www.")
        p_path = p_parsed.path or "/"
        p_query = p_parsed.query

        if (
            target_host_apex == p_host
            and target_path == p_path
            and target_query == p_query
        ):
            www_matches.append(p)

    if len(www_matches) == 1:
        return www_matches[0]

    # Tier 4: Unambiguous trailing-slash mismatch
    # (e.g. target is '/pricing' and pages has only '/pricing/', or vice versa)
    # ONLY matches if:
    # 1. target_host matches page host
    # 2. target_query matches page query
    # 3. Path differs only by trailing slash
    # 4. EXACTLY ONE candidate in pages matches (if both exist, count > 1 -> fails closed!)
    slash_alt_path = target_path[:-1] if target_path.endswith("/") and target_path != "/" else (target_path + "/")
    slash_matches: list[PageContent] = []
    for p in pages:
        p_canon = canonicalize_url(p.url)
        p_parsed = urlparse(p_canon)
        p_host = (p_parsed.hostname or "").removeprefix("www.")
        p_path = p_parsed.path or "/"
        p_query = p_parsed.query

        if (
            target_host_apex == p_host
            and slash_alt_path == p_path
            and target_query == p_query
        ):
            slash_matches.append(p)

    if len(slash_matches) == 1:
        return slash_matches[0]

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


def is_service_name_supported_by_quote(service_name: str, quote: str) -> bool:
    """Validate whether an evidence quotation establishes the specific service name.

    Conservative deterministic name-to-evidence validation:
    - Normalizes both strings.
    - Checks whether the service name (or its core noun phrase) appears in the quote as a phrase.
    - Allows 'web' and 'website' equivalence.
    - Handles generic service suffixes like 'services', 'solutions', 'offerings'.
    - Rejects broad or unrelated quotations (e.g. 'Business Registration' supported only
      by 'Brand Trademark Registration').
    """
    if not service_name or not quote:
        return False

    norm_name = normalize_text_for_matching(service_name).strip(_STRIP_PUNCT)
    norm_quote = normalize_text_for_matching(quote).strip(_STRIP_PUNCT)

    if not norm_name or not norm_quote:
        return False

    # 1. Exact match
    if norm_name == norm_quote:
        return True

    # Web / Website equivalence
    norm_name_web = re.sub(r"\bwebsites?\b", "web", norm_name)
    norm_quote_web = re.sub(r"\bwebsites?\b", "web", norm_quote)
    if norm_name_web == norm_quote_web:
        return True

    # 2. Service name is contained as a word-bounded phrase in the quote
    # e.g. quote="We provide custom website development for businesses", name="website development"
    pattern = r"(?:\b|^)" + re.escape(norm_name) + r"(?:\b|$)"
    if re.search(pattern, norm_quote):
        return True

    pattern_web = r"(?:\b|^)" + re.escape(norm_name_web) + r"(?:\b|$)"
    if re.search(pattern_web, norm_quote_web):
        return True

    # 3. Handle common generic service descriptor suffixes
    # e.g. name="Printing Services", quote="High quality printing for your brand"
    generic_suffixes = ("services", "service", "solutions", "solution", "offerings", "offering")
    core_name = norm_name
    for sfx in generic_suffixes:
        if core_name.endswith(" " + sfx):
            core_name = core_name[:-len(sfx)-1].strip()
            break

    if len(core_name) >= 4:
        core_pattern = r"(?:\b|^)" + re.escape(core_name) + r"(?:\b|$)"
        if re.search(core_pattern, norm_quote):
            return True

    # 4. Heading quote contained in service name (e.g. quote="Logo Design", name="Professional Logo Design")
    # Only if quote is at least 2 words or >= 8 chars to avoid trivial single-word overlaps
    quote_words = norm_quote.split()
    if len(quote_words) >= 2 or len(norm_quote) >= 8:
        if re.search(r"(?:\b|^)" + re.escape(norm_quote) + r"(?:\b|$)", norm_name):
            return True

    return False


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


def find_brand_name_evidence(brand_name: str, pages: list[PageContent]) -> list[Evidence]:
    """Find inspectable evidence verifying the brand name from page metadata/content."""
    if not brand_name or not pages:
        return []
    norm_name = normalize_text_for_matching(brand_name)
    if len(norm_name) < 2:
        return []

    ev_list: list[Evidence] = []
    for p in pages:
        # 1. OpenGraph site_name
        og_site = p.og.get("og:site_name", "")
        if og_site:
            norm_og = normalize_text_for_matching(og_site)
            if norm_name in norm_og or norm_og in norm_name:
                ev_list.append(Evidence(quote=og_site, source_url=p.url))
                break

        # 2. Page title
        if p.title:
            norm_title = normalize_text_for_matching(p.title)
            if re.search(r"(?:\b|^)" + re.escape(norm_name) + r"(?:\b|$)", norm_title):
                ev_list.append(Evidence(quote=p.title, source_url=p.url))
                break

        # 3. OpenGraph title
        og_title = p.og.get("og:title", "")
        if og_title:
            norm_og_t = normalize_text_for_matching(og_title)
            if re.search(r"(?:\b|^)" + re.escape(norm_name) + r"(?:\b|$)", norm_og_t):
                ev_list.append(Evidence(quote=og_title, source_url=p.url))
                break

        # 4. H1 or Headings
        found_heading = False
        for h in p.h1 + p.headings:
            norm_h = normalize_text_for_matching(h)
            if re.search(r"(?:\b|^)" + re.escape(norm_name) + r"(?:\b|$)", norm_h):
                ev_list.append(Evidence(quote=h, source_url=p.url))
                found_heading = True
                break
        if found_heading:
            break

        # 5. Blocks
        found_block = False
        for b in p.blocks:
            norm_b = normalize_text_for_matching(b)
            if re.search(r"(?:\b|^)" + re.escape(norm_name) + r"(?:\b|$)", norm_b):
                clean_b = re.sub(r"^#+\s*", "", b).strip()
                ev_list.append(Evidence(quote=clean_b[:200], source_url=p.url))
                found_block = True
                break
        if found_block:
            break

    return ev_list


def is_brand_name_verified(brand_name: str, pages: list[PageContent]) -> bool:
    """Verify whether the brand name is grounded in the website pages."""
    return len(find_brand_name_evidence(brand_name, pages)) > 0


def is_summary_fully_supported(summary: str, evidence: list[Evidence]) -> bool:
    """Validate whether an entire brand summary is supported by the evidence.

    - If the summary makes multiple distinct claims or lists offerings, all material
      claims must be supported across the evidence quotations.
    - If any material claim is missing from the evidence, returns False.
    - If evidence is empty, returns False.
    """
    if not summary or not evidence:
        return False

    norm_summary = normalize_text_for_matching(summary)
    evidence_text = " ".join(normalize_text_for_matching(ev.quote) for ev in evidence)

    if not evidence_text:
        return False

    # 1. Full verbatim match
    if norm_summary in evidence_text:
        return True

    # 2. Check for multi-claim list items in summary (e.g. "specializes in A, B, C, D and E")
    if "," in summary:
        raw_items = re.split(r"[,;]|\b(?:and|as well as|along with)\b", summary, flags=re.I)
        cleaned_items: list[str] = []
        for it in raw_items:
            norm_it = normalize_text_for_matching(it).strip(_STRIP_PUNCT)
            norm_it = re.sub(
                r"^(?:(?:the|our|your|a|an)\s+)?(?:business|company|brand|agency|team|innovations|studio)?\s*"
                r"(?:specializes in|provides|offers|includes|focuses on|delivers|excels in|services in|solutions for)?\s*",
                "",
                norm_it,
            ).strip(_STRIP_PUNCT)
            if len(norm_it) >= 5 and norm_it not in cleaned_items:
                cleaned_items.append(norm_it)

        if len(cleaned_items) >= 2:
            # Multi-claim list: every item must be present in evidence_text
            for item in cleaned_items:
                item_words = item.split()
                if len(item_words) >= 2:
                    phrase_match = re.search(r"(?:\b|^)" + re.escape(item) + r"(?:\b|$)", evidence_text)
                    if not phrase_match:
                        core = " ".join(item_words[:2])
                        if not re.search(r"(?:\b|^)" + re.escape(core) + r"(?:\b|$)", evidence_text):
                            return False
                else:
                    if not re.search(r"(?:\b|^)" + re.escape(item) + r"(?:\b|$)", evidence_text):
                        return False
            return True

    # 3. For single-claim summaries, check if the core statement is supported
    summary_words = [w for w in norm_summary.split() if len(w) >= 4]
    if not summary_words:
        return False
    matched_words = sum(1 for w in summary_words if re.search(r"\b" + re.escape(w) + r"\b", evidence_text))
    return (matched_words / len(summary_words)) >= 0.5


def ground_brand(brand: Brand, pages: list[PageContent]) -> Brand:
    """Validate brand evidence and set brand name, summary, and overall provenance.

    Guarantees:
    - Verified brand name exposes inspectable evidence in name_evidence.
    - If brand summary contains unsupported claims, summary_provenance is inferred.
    - Overall brand provenance is website_verified ONLY if both name and summary are verified.
    - Verified evidence objects point to actual fetched page URLs.
    """
    name_evidence = find_brand_name_evidence(brand.name, pages)
    name_prov: Provenance = "website_verified" if name_evidence else "inferred"

    valid_summary_ev: list[Evidence] = []
    for ev in brand.evidence:
        canonical_ev = verify_and_canonicalize_evidence(ev, pages)
        if canonical_ev and not is_generic_boilerplate_quote(canonical_ev.quote, brand.name):
            valid_summary_ev.append(canonical_ev)

    is_supported = is_summary_fully_supported(brand.summary, valid_summary_ev)
    summary_prov: Provenance = "website_verified" if (valid_summary_ev and is_supported) else "inferred"

    overall_prov: Provenance = "website_verified" if (name_prov == "website_verified" and summary_prov == "website_verified") else "inferred"

    summary_evidence_out = valid_summary_ev[:2] if valid_summary_ev else []

    return brand.model_copy(update={
        "provenance": overall_prov,
        "name_provenance": name_prov,
        "name_evidence": name_evidence[:2],
        "summary_provenance": summary_prov,
        "evidence": summary_evidence_out,
    })


def ground_service(service: Service, pages: list[PageContent]) -> Service:
    """Validate a service's evidence and establish its factual provenance and description provenance.

    Guarantees:
    - Service name must be supported by the actual quotation.
    - If evidence does not establish the service name, provenance is downgraded to inferred.
    - description_provenance is verified if the description appears verbatim in the page corpus.
    - Verified evidence objects point to the actual fetched page URL.
    """
    # Verify quotation exists on fetched page
    valid_ev = [
        canonical_ev
        for ev in service.evidence
        if (canonical_ev := verify_and_canonicalize_evidence(ev, pages)) is not None
    ]

    # Verify that at least one quotation supports the service name
    valid_name_ev = [
        ev for ev in valid_ev
        if is_service_name_supported_by_quote(service.name, ev.quote)
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
        if valid_name_ev:
            return service.model_copy(update={
                "source": "website",
                "provenance": "website_verified",
                "description_provenance": desc_prov,
                "evidence": valid_ev[:2],
            })
        else:
            log.info("downgrading service '%s' to inferred (evidence does not support service name)", service.name)
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
            "description_provenance": desc_prov if valid_name_ev else "inferred",
            "evidence": valid_ev[:2] if valid_name_ev else [],
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
