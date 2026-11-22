"""Tests for Phase 3.2.2: Claim-Level Evidence Accuracy.

Covers:
1. Service name matching its evidence quotation is verified.
2. Service name not supported by an otherwise valid quotation is downgraded.
3. "Business Registration" cannot be verified solely by "Brand Trademark Registration".
4. A synthesized service description remains inferred when only the service name is supported.
5. A multi-claim brand summary with incomplete evidence is inferred.
6. A fully supported brand summary can retain verified provenance.
7. A verified brand name exposes its supporting evidence (Plausible-style metadata extraction).
8. Missing brand-name evidence cannot retain verified provenance.
9. /pricing and /pricing/ cannot produce incorrect cross-page attribution.
10. Repeated query parameters and fragment normalization preserve collision safety.
"""
import pytest

from app.schemas import Brand, Evidence, Service
from app.services.grounding import (
    canonicalize_url,
    find_brand_name_evidence,
    find_matching_page,
    ground_brand,
    ground_service,
    is_brand_name_verified,
    is_service_name_supported_by_quote,
    is_summary_fully_supported,
    validate_evidence,
)
from app.services.website_extractor import PageContent


# -----------------------------------------------------------------------------
# 1. Service name matching its evidence quotation is verified
# -----------------------------------------------------------------------------
def test_service_name_matching_evidence_quote_is_verified():
    page = PageContent(
        url="https://example.com/services",
        blocks=["# Brand Trademark Registration", "Full legal protection for intellectual property."],
    )
    service = Service(
        name="Brand Trademark Registration",
        description="Comprehensive trademark registration services in India.",
        target_industry="Businesses",
        target_customer="Founders",
        main_benefit="Legal protection",
        source="website",
        evidence=[
            Evidence(
                quote="Brand Trademark Registration",
                source_url="https://example.com/services",
            )
        ],
    )
    grounded = ground_service(service, [page])
    assert grounded.source == "website"
    assert grounded.provenance == "website_verified"
    assert len(grounded.evidence) == 1


# -----------------------------------------------------------------------------
# 2. Service name not supported by an otherwise valid quotation is downgraded
# -----------------------------------------------------------------------------
def test_service_name_not_supported_by_valid_quote_is_downgraded():
    page = PageContent(
        url="https://example.com/",
        blocks=["Contact our customer support team at +1 800 555 0199 for billing inquiries."],
    )
    # The quote exists on the page, but has nothing to do with "SEO Audit & Strategy"
    service = Service(
        name="SEO Audit & Strategy",
        description="In-depth search optimization audit.",
        target_industry="E-commerce",
        target_customer="Marketing Directors",
        main_benefit="Organic traffic growth",
        source="website",
        evidence=[
            Evidence(
                quote="Contact our customer support team at +1 800 555 0199 for billing inquiries",
                source_url="https://example.com/",
            )
        ],
    )
    grounded = ground_service(service, [page])
    assert grounded.source == "inferred"
    assert grounded.provenance == "inferred"
    assert grounded.evidence == []


# -----------------------------------------------------------------------------
# 3. "Business Registration" cannot be verified solely by "Brand Trademark Registration"
# -----------------------------------------------------------------------------
def test_business_registration_cannot_be_verified_by_trademark_registration():
    page = PageContent(
        url="https://www.jayjagleinnovations.com/",
        blocks=["# Brand Trademark Registration", "Protect your logo and company identity."],
    )
    assert is_service_name_supported_by_quote("Business Registration", "Brand Trademark Registration") is False

    service = Service(
        name="Business Registration",
        description="Company incorporation and legal business entity setup.",
        target_industry="Startups",
        target_customer="Founders",
        main_benefit="Formal incorporation",
        source="website",
        evidence=[
            Evidence(
                quote="Brand Trademark Registration",
                source_url="https://www.jayjagleinnovations.com/",
            )
        ],
    )
    grounded = ground_service(service, [page])
    # Must be downgraded to inferred because the quote does not establish "Business Registration"
    assert grounded.source == "inferred"
    assert grounded.provenance == "inferred"
    assert grounded.evidence == []


# -----------------------------------------------------------------------------
# 4. Synthesized description remains inferred when only service name is supported
# -----------------------------------------------------------------------------
def test_synthesized_description_remains_inferred_when_name_supported():
    page = PageContent(
        url="https://example.com/",
        blocks=["# Website Development", "We build fast sites."],
    )
    service = Service(
        name="Website Development",
        # Highly elaborated AI-synthesized description not appearing in page text
        description="End-to-end fullstack React and Node.js enterprise microservices architecture.",
        target_industry="Tech",
        target_customer="Enterprises",
        main_benefit="Scalability",
        source="website",
        evidence=[
            Evidence(
                quote="Website Development",
                source_url="https://example.com/",
            )
        ],
    )
    grounded = ground_service(service, [page])
    assert grounded.source == "website"
    assert grounded.provenance == "website_verified"
    assert grounded.description_provenance == "inferred"


# -----------------------------------------------------------------------------
# 5. Multi-claim brand summary with incomplete evidence is inferred
# -----------------------------------------------------------------------------
def test_multi_claim_brand_summary_with_incomplete_evidence_is_inferred():
    page = PageContent(
        url="https://example.com/",
        title="Jay Jagle Innovations | Marketing Agency",
        blocks=[
            "Jay Jagle Innovations specializes in Business registration, logo design, "
            "social media marketing, and printing services.",
        ],
    )
    # Summary claims 5 offerings including "website development" which is omitted from the quote
    brand = Brand(
        name="Jay Jagle Innovations",
        category="Digital Marketing",
        summary="Jay Jagle Innovations specializes in business registration, logo design, "
                "social media marketing, website development and printing services.",
        evidence=[
            Evidence(
                quote="Jay Jagle Innovations specializes in Business registration, logo design, "
                      "social media marketing, and printing services.",
                source_url="https://example.com/",
            )
        ],
    )
    grounded = ground_brand(brand, [page])
    assert grounded.name_provenance == "website_verified"
    # Because "website development" is missing from the quotation, summary must be inferred
    assert grounded.summary_provenance == "inferred"
    # Overall brand provenance must not overstate certainty
    assert grounded.provenance == "inferred"


# -----------------------------------------------------------------------------
# 6. Fully supported brand summary can retain verified provenance
# -----------------------------------------------------------------------------
def test_fully_supported_brand_summary_retains_verified_provenance():
    page = PageContent(
        url="https://example.com/",
        title="Jay Jagle Innovations | Full Service Agency",
        blocks=[
            "Jay Jagle Innovations specializes in Business registration, logo design, "
            "social media marketing, and printing services.",
            "We are also a premier website development agency in Surat.",
        ],
    )
    brand = Brand(
        name="Jay Jagle Innovations",
        category="Digital Marketing",
        summary="Jay Jagle Innovations specializes in business registration, logo design, "
                "social media marketing, website development and printing services.",
        evidence=[
            Evidence(
                quote="Jay Jagle Innovations specializes in Business registration, logo design, "
                      "social media marketing, and printing services.",
                source_url="https://example.com/",
            ),
            Evidence(
                quote="We are also a premier website development agency in Surat.",
                source_url="https://example.com/",
            ),
        ],
    )
    grounded = ground_brand(brand, [page])
    assert grounded.name_provenance == "website_verified"
    assert grounded.summary_provenance == "website_verified"
    assert grounded.provenance == "website_verified"
    assert len(grounded.evidence) == 2


# -----------------------------------------------------------------------------
# 7. Verified brand name exposes its supporting evidence (Plausible-style)
# -----------------------------------------------------------------------------
def test_verified_brand_name_exposes_inspectable_evidence():
    # Plausible-style case: brand name is verified via page title and OG tags
    page = PageContent(
        url="https://plausible.io/",
        title="Plausible Analytics | Simple and privacy-friendly Google Analytics alternative",
        og={
            "og:site_name": "Plausible Analytics",
            "og:title": "Plausible Analytics | Simple and privacy-friendly Google Analytics alternative",
        },
        blocks=["Plausible is a lightweight and open-source website analytics tool."],
    )
    # LLM outputs brand with empty evidence for summary
    brand = Brand(
        name="Plausible Analytics",
        category="Web Analytics",
        summary="Privacy-friendly open source web analytics platform.",
        evidence=[],
    )
    grounded = ground_brand(brand, [page])
    assert grounded.name_provenance == "website_verified"
    # Must expose inspectable evidence for the brand name
    assert len(grounded.name_evidence) >= 1
    assert grounded.name_evidence[0].quote == "Plausible Analytics"
    assert grounded.name_evidence[0].source_url == "https://plausible.io/"


# -----------------------------------------------------------------------------
# 8. Missing brand-name evidence cannot retain verified provenance
# -----------------------------------------------------------------------------
def test_missing_brand_name_evidence_cannot_retain_verified_provenance():
    page = PageContent(
        url="https://example.com/",
        title="General Services Portal",
        blocks=["Welcome to our corporate website."],
    )
    brand = Brand(
        name="Completely Fabricated Brand Corp",
        category="Unknown",
        summary="Global conglomerate.",
        evidence=[],
    )
    grounded = ground_brand(brand, [page])
    assert grounded.name_provenance == "inferred"
    assert grounded.name_evidence == []
    assert grounded.provenance == "inferred"


# -----------------------------------------------------------------------------
# 9. /pricing and /pricing/ cannot produce incorrect cross-page attribution
# -----------------------------------------------------------------------------
def test_trailing_slash_collision_prevention():
    page_without_slash = PageContent(
        url="https://example.com/pricing",
        title="Pricing Without Slash",
        blocks=["Monthly subscription starts at $19 per user."],
    )
    page_with_slash = PageContent(
        url="https://example.com/pricing/",
        title="Pricing With Slash",
        blocks=["Annual enterprise plan starts at $999 per organization."],
    )
    pages = [page_without_slash, page_with_slash]

    # Verify canonical URLs are distinct
    assert canonicalize_url("https://example.com/pricing") != canonicalize_url("https://example.com/pricing/")

    # 1. Exact match for page without slash
    ev_no_slash = Evidence(
        quote="Monthly subscription starts at $19 per user.",
        source_url="https://example.com/pricing",
    )
    assert validate_evidence(ev_no_slash, pages) is True
    matched_no_slash = find_matching_page("https://example.com/pricing", pages)
    assert matched_no_slash is not None and matched_no_slash.url == "https://example.com/pricing"

    # 2. Exact match for page with slash
    ev_with_slash = Evidence(
        quote="Annual enterprise plan starts at $999 per organization.",
        source_url="https://example.com/pricing/",
    )
    assert validate_evidence(ev_with_slash, pages) is True
    matched_with_slash = find_matching_page("https://example.com/pricing/", pages)
    assert matched_with_slash is not None and matched_with_slash.url == "https://example.com/pricing/"

    # 3. Cross-attribution must fail: quote from with_slash cannot be validated against no_slash URL
    ev_cross_1 = Evidence(
        quote="Annual enterprise plan starts at $999 per organization.",
        source_url="https://example.com/pricing",  # Wrong page!
    )
    assert validate_evidence(ev_cross_1, pages) is False

    # 4. Cross-attribution must fail: quote from no_slash cannot be validated against with_slash URL
    ev_cross_2 = Evidence(
        quote="Monthly subscription starts at $19 per user.",
        source_url="https://example.com/pricing/",  # Wrong page!
    )
    assert validate_evidence(ev_cross_2, pages) is False


# -----------------------------------------------------------------------------
# 10. Repeated query parameters and URL canonicalization
# -----------------------------------------------------------------------------
def test_repeated_query_parameters_sorting():
    # Repeated keys with different ordering must canonicalize to identical strings
    url1 = "https://example.com/search?tag=python&tag=async&sort=desc"
    url2 = "https://example.com/search?sort=desc&tag=async&tag=python"
    assert canonicalize_url(url1) == canonicalize_url(url2)

    page = PageContent(
        url="https://example.com/search?sort=desc&tag=async&tag=python",
        title="Search Results",
        blocks=["Found 42 matching articles on asynchronous programming."],
    )
    ev = Evidence(
        quote="Found 42 matching articles on asynchronous programming.",
        source_url="https://example.com/search?tag=python&tag=async&sort=desc",
    )
    assert validate_evidence(ev, [page]) is True
