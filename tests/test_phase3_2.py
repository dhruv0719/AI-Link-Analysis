"""Tests for Phase 3.2: Runtime Grounding & Evidence Provenance.

Covers:
1. Evidence validation (exact quote, whitespace normalization, fabricated quote rejection,
   unfetched URL rejection, cross-page misattribution rejection, empty evidence rejection).
2. Service provenance (retains source="website" with valid evidence, downgrades to "inferred"
   when evidence is missing or invalid, genuinely inferred stays inferred, invalid evidence not presented as verified).
3. Geographic provenance (explicit address marked website-verified, unsupported claim marked inferred,
   missing geographic info remains None).
4. Recommendations and inferences (recommendations do not require factual source quotations,
   inferred customer profiles are not represented as website facts).
"""
import pytest
from pydantic import ValidationError

from app.schemas import (
    Brand,
    Evidence,
    MarketingSegment,
    SearchQuery,
    Service,
    SupportingContent,
    TargetAudience,
)
from app.services.grounding import (
    find_matching_page,
    ground_brand,
    ground_service,
    ground_target_audience,
    normalize_text_for_matching,
    validate_evidence,
)
from app.services.website_extractor import PageContent


# -----------------------------------------------------------------------------
# Fixtures & Helpers
# -----------------------------------------------------------------------------
def _sample_pages() -> list[PageContent]:
    page1 = PageContent(
        url="https://acme.example/",
        title="Acme Web Studio | Affordable Websites for Small Businesses",
        meta_description="Acme Web Studio builds fast, affordable websites and online stores for small businesses in Surat.",
        h1=["We build websites that win customers"],
        headings=["Custom website development for small businesses", "E-commerce stores on Shopify and WooCommerce"],
        blocks=[
            "Acme Web Studio is a web development agency helping small businesses and startups get online.",
            "We offer custom website development for small businesses and e-commerce stores.",
        ],
        footer_text="© 2026 Acme Web Studio Ltd. 100 Ring Road, Surat, Gujarat 395001, India. Tel: +91 261 555 0199",
    )
    page2 = PageContent(
        url="https://acme.example/pricing",
        title="Pricing & Retainers | Acme Web Studio",
        h1=["Simple, Transparent Retainers"],
        headings=["Monthly Maintenance Retainer"],
        blocks=[
            "Plans start at $99 per month for full security updates, cloud backups, and regular content updates.",
            "Dedicated support for growing digital brands.",
        ],
    )
    return [page1, page2]


# =============================================================================
# 1. Evidence Validation Tests
# =============================================================================

def test_exact_valid_quotation_from_fetched_page_is_accepted():
    pages = _sample_pages()
    ev = Evidence(
        quote="We build websites that win customers",
        source_url="https://acme.example/",
    )
    assert validate_evidence(ev, pages) is True


def test_quotation_with_different_whitespace_is_accepted_after_normalization():
    pages = _sample_pages()
    # Quote with multiple newlines and tab spacing
    ev = Evidence(
        quote="Acme Web Studio is a web development\n\n  agency helping\tsmall businesses",
        source_url="https://acme.example/",
    )
    assert validate_evidence(ev, pages) is True


def test_fabricated_quotation_is_rejected():
    pages = _sample_pages()
    ev = Evidence(
        quote="We build advanced quantum computing AI pipelines for space exploration",
        source_url="https://acme.example/",
    )
    assert validate_evidence(ev, pages) is False


def test_unfetched_evidence_url_is_rejected():
    pages = _sample_pages()
    ev = Evidence(
        quote="We build websites that win customers",
        source_url="https://acme.example/unfetched-blog-post",
    )
    assert validate_evidence(ev, pages) is False


def test_cross_page_misattribution_is_rejected():
    pages = _sample_pages()
    # Quote only exists on page2 (/pricing), but LLM claims it came from page1 (homepage)
    ev = Evidence(
        quote="Plans start at $99 per month for full security updates",
        source_url="https://acme.example/",  # Wrong page!
    )
    assert validate_evidence(ev, pages) is False

    # When attributed to the correct page, it must pass
    ev_correct = Evidence(
        quote="Plans start at $99 per month for full security updates",
        source_url="https://acme.example/pricing",
    )
    assert validate_evidence(ev_correct, pages) is True


def test_empty_or_whitespace_evidence_is_rejected():
    with pytest.raises(ValidationError):
        Evidence(quote="   ", source_url="https://acme.example/")

    with pytest.raises(ValidationError):
        Evidence(quote="Valid quote", source_url="   ")


# =============================================================================
# 2. Service Provenance Tests
# =============================================================================

def test_correctly_evidenced_website_service_retains_website_provenance():
    pages = _sample_pages()
    service = Service(
        name="Custom website development",
        description="Fast, tailored business websites built to convert visitors.",
        target_industry="Small business",
        target_customer="Founders",
        main_benefit="Professional presence",
        source="website",
        evidence=[
            Evidence(
                quote="Custom website development for small businesses",
                source_url="https://acme.example/",
            )
        ],
    )
    grounded = ground_service(service, pages)
    assert grounded.source == "website"
    assert grounded.provenance == "website_verified"
    assert len(grounded.evidence) == 1
    assert grounded.evidence[0].quote == "Custom website development for small businesses"


def test_unsupported_service_marked_website_is_downgraded_to_inferred():
    pages = _sample_pages()
    # Service marked website, but evidence is completely fabricated
    service = Service(
        name="Blockchain & Web3 Consulting",
        description="Smart contract development and audit.",
        target_industry="Crypto",
        target_customer="Founders",
        main_benefit="Decentralization",
        source="website",
        evidence=[
            Evidence(
                quote="We audit smart contracts on Ethereum and Solana",
                source_url="https://acme.example/",
            )
        ],
    )
    grounded = ground_service(service, pages)
    assert grounded.source == "inferred"
    assert grounded.provenance == "inferred"
    assert grounded.evidence == []  # Invalid evidence must not be presented as verified


def test_service_marked_website_with_empty_evidence_is_downgraded():
    pages = _sample_pages()
    service = Service(
        name="Website maintenance",
        description="Updates and security retainers.",
        target_industry="SMEs",
        target_customer="Owners",
        main_benefit="Peace of mind",
        source="website",
        evidence=[],  # No evidence provided!
    )
    grounded = ground_service(service, pages)
    assert grounded.source == "inferred"
    assert grounded.provenance == "inferred"


def test_genuinely_inferred_service_remains_inferred():
    pages = _sample_pages()
    service = Service(
        name="Digital Transformation Strategy",
        description="Strategic consulting for modernizing legacy workflows.",
        target_industry="General",
        target_customer="Executives",
        main_benefit="Modern tech stack",
        source="inferred",
        evidence=[],
    )
    grounded = ground_service(service, pages)
    assert grounded.source == "inferred"
    assert grounded.provenance == "inferred"


def test_invalid_evidence_is_never_promoted_or_retained():
    pages = _sample_pages()
    service = Service(
        name="Custom website development",
        description="Tailored websites.",
        target_industry="SMB",
        target_customer="Owners",
        main_benefit="Growth",
        source="website",
        evidence=[
            Evidence(
                quote="Fabricated statement not on any page",
                source_url="https://acme.example/",
            )
        ],
    )
    grounded = ground_service(service, pages)
    # Must be downgraded and evidence cleared
    assert grounded.source == "inferred"
    assert grounded.provenance == "inferred"
    assert len(grounded.evidence) == 0


# =============================================================================
# 3. Geographic Provenance Tests
# =============================================================================

def test_explicit_address_supported_by_content_is_marked_website_verified():
    pages = _sample_pages()
    audience = TargetAudience(
        primary_audience=["Small business owners"],
        geographic_focus="Surat, Gujarat, India",
        geographic_focus_evidence=[
            Evidence(
                quote="100 Ring Road, Surat, Gujarat 395001, India",
                source_url="https://acme.example/",
            )
        ],
    )
    grounded = ground_target_audience(audience, pages)
    assert grounded.geographic_focus == "Surat, Gujarat, India"
    assert grounded.geographic_focus_provenance == "website_verified"
    assert len(grounded.geographic_focus_evidence) == 1


def test_unsupported_geographic_claim_is_marked_inferred():
    pages = _sample_pages()
    # Claiming New York with fabricated or missing evidence
    audience = TargetAudience(
        primary_audience=["Small business owners"],
        geographic_focus="New York, NY, USA",
        geographic_focus_evidence=[
            Evidence(
                quote="Located at 5th Avenue, New York, NY",
                source_url="https://acme.example/",
            )
        ],
    )
    grounded = ground_target_audience(audience, pages)
    assert grounded.geographic_focus == "New York, NY, USA"
    assert grounded.geographic_focus_provenance == "inferred"
    assert grounded.geographic_focus_evidence == []


def test_missing_geographic_information_remains_none():
    pages = _sample_pages()
    audience = TargetAudience(
        primary_audience=["Global developers"],
        geographic_focus=None,
        geographic_focus_evidence=[],
    )
    grounded = ground_target_audience(audience, pages)
    assert grounded.geographic_focus is None
    assert grounded.geographic_focus_provenance is None
    assert grounded.geographic_focus_evidence == []


# =============================================================================
# 4. Recommendations & Inferences Tests
# =============================================================================

def test_recommendations_do_not_require_factual_source_quotations():
    # Search queries and supporting content are recommendations by definition
    query = SearchQuery(
        query="affordable website development agency Surat",
        platforms=["Google", "LinkedIn"],
        intent="commercial",
        business_relevance="Direct match for core web development offering",
        target_audience="Small business owners",
    )
    assert query.provenance == "recommendation"

    content = SupportingContent(
        type="service_landing_page",
        title="Custom WordPress & Shopify Web Development in Surat",
        rationale="Targets local high-intent transactional search terms",
    )
    assert content.provenance == "recommendation"

    segment = MarketingSegment(
        audience="Local retail shop owners",
        business_type="B2C Retail",
        requirement="Quick turn-around e-commerce setup",
    )
    assert segment.provenance == "recommendation"


def test_inferred_customer_profiles_are_not_represented_as_website_verified_facts():
    pages = _sample_pages()
    audience = TargetAudience(
        primary_audience=["Small business owners", "Startup founders"],
        customer_profiles=[
            "First-time founder seeking affordable MVP web presence",
            "Local shopkeeper expanding into digital sales",
        ],
        customer_needs=["Low upfront cost", "Reliable ongoing technical support"],
        geographic_focus="Surat",
        geographic_focus_evidence=[
            Evidence(
                quote="small businesses in Surat",
                source_url="https://acme.example/",
            )
        ],
    )
    grounded = ground_target_audience(audience, pages)
    # Geographic location can be verified, but audience profile remains explicitly inferred
    assert grounded.geographic_focus_provenance == "website_verified"
    assert grounded.audience_provenance == "inferred"


def test_brand_provenance_verification():
    pages = _sample_pages()
    # Brand with valid quote from homepage
    brand_valid = Brand(
        name="Acme Web Studio",
        category="Web Development",
        summary="Web agency in Surat.",
        evidence=[
            Evidence(
                quote="Acme Web Studio is a web development agency",
                source_url="https://acme.example/",
            )
        ],
    )
    grounded_brand = ground_brand(brand_valid, pages)
    assert grounded_brand.provenance == "website_verified"
    assert len(grounded_brand.evidence) == 1

    # Brand with invalid quote
    brand_invalid = Brand(
        name="Global Acme Megacorp",
        category="Aerospace",
        summary="Defense contractor.",
        evidence=[
            Evidence(
                quote="Global Acme Megacorp is the world leader in aerospace defense",
                source_url="https://acme.example/",
            )
        ],
    )
    grounded_invalid = ground_brand(brand_invalid, pages)
    assert grounded_invalid.provenance == "inferred"
    assert grounded_invalid.evidence == []
