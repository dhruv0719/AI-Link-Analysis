"""Tests for Phase 3.2.1: Grounding Reliability & Coverage Fixes.

Covers the 9 required audit and reliability scenarios:
1. Real-like homepage with services only in headings (Jay Jagle style) correctly grounded.
2. Exact matching works for verbatim quotes with formatting/spacing/HTML artifacts.
3. Fabricated quotes are rejected and downgraded.
4. Quotes attributed to the wrong page are rejected.
5. Service supported by body paragraph has verified offering and verified description.
6. Service with elaborated description has verified offering but inferred description.
7. Brand verified from title/OG metadata sets name_provenance="website_verified".
8. Generic boilerplate quote rejected for brand verification unless naming the brand.
9. Local Jay Jagle fixture integration test validating all 5 services ground without false negatives.
"""
import pytest

from app.schemas import Brand, Evidence, Service, TargetAudience
from app.services.grounding import (
    find_matching_page,
    ground_brand,
    ground_service,
    ground_target_audience,
    is_brand_name_verified,
    is_generic_boilerplate_quote,
    normalize_text_for_matching,
    validate_evidence,
)
from app.services.website_extractor import PageContent


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
def _jayjagle_pages() -> list[PageContent]:
    """Realistic page content extracted from Jay Jagle Innovations."""
    home = PageContent(
        url="https://www.jayjagleinnovations.com/",
        title="Jay Jagle Innovations | Digital Marketing & Web Development Agency",
        meta_description=(
            "Jay Jagle Innovations is one stop for all kinds of Printing Services"
            "(Visiting Card, Letterheads, Envelopes)to help you establish your brand."
        ),
        h1=[
            "Social Media Marketing",
            "Website Development",
            "Professional Logo Design",
            "Brand Trademark Registration",
            "High quality printing for your brand",
        ],
        headings=[
            "Social Media Marketing",
            "Website Development",
            "Professional Logo Design",
            "Brand Trademark Registration",
            "High quality printing for your brand",
        ],
        blocks=[
            "# Social Media Marketing",
            "# Website Development",
            "# Professional Logo Design",
            "# Brand Trademark Registration",
            "# High quality printing for your brand",
            "Jay Jagle Innovations is one stop for all kinds of Printing Services"
            "(Visiting Card, Letterheads, Envelopes)to help you establish your brand.",
        ],
        footer_text="© 2026 Jay Jagle Innovations. All rights reserved. Privacy Policy | Terms of Service",
        og={
            "og:site_name": "Jay Jagle Innovations",
            "og:title": "Jay Jagle Innovations | Digital Marketing & Web Development",
        },
    )
    contact = PageContent(
        url="https://www.jayjagleinnovations.com/contact",
        title="Contact Us | Jay Jagle Innovations",
        blocks=[
            "# Contact Information",
            "Visit our office or call us today.",
            "3rd Floor, Shop No. 334/B, G9 Business Centre, Karadva Road, Dindoli, Surat - 394210, Gujarat, India.",
        ],
        footer_text="© 2026 Jay Jagle Innovations. All rights reserved.",
    )
    return [home, contact]


# =============================================================================
# 1. Real-like homepage with services only in headings (Jay Jagle style)
# =============================================================================
def test_heading_only_services_ground_successfully():
    pages = _jayjagle_pages()
    service = Service(
        name="Social Media Marketing",
        description="Comprehensive social media management, organic growth, and paid campaigns.",
        target_industry="E-commerce and local businesses",
        target_customer="Business owners",
        main_benefit="Increased brand visibility and lead generation",
        source="website",
        evidence=[
            Evidence(
                quote="Social Media Marketing",
                source_url="https://jayjagleinnovations.com",  # Non-www request URL
            )
        ],
    )
    grounded = ground_service(service, pages)
    assert grounded.source == "website"
    assert grounded.provenance == "website_verified"
    assert grounded.description_provenance == "inferred"  # AI synthesized description
    assert len(grounded.evidence) == 1
    assert grounded.evidence[0].quote == "Social Media Marketing"


# =============================================================================
# 2. Exact matching works for verbatim quotes with varied formatting/spacing
# =============================================================================
def test_exact_matching_with_formatting_and_html_artifacts():
    pages = _jayjagle_pages()
    # In HTML: "(Visiting Card, Letterheads, Envelopes)to help" without space after ")"
    # LLM quote includes proper space: "(Visiting Card, Letterheads, Envelopes) to help"
    ev = Evidence(
        quote="Printing Services (Visiting Card, Letterheads, Envelopes) to help you establish your brand",
        source_url="https://www.jayjagleinnovations.com/",
    )
    assert validate_evidence(ev, pages) is True

    # Smart quotes, zero-width spaces, and extra whitespace
    norm1 = normalize_text_for_matching("“Special ‘Quotes’ & — Dashes”\u200b")
    norm2 = normalize_text_for_matching('"Special \'Quotes\' & - Dashes"')
    assert norm1 == norm2


# =============================================================================
# 3. Fabricated quotes are rejected and downgraded
# =============================================================================
def test_fabricated_quotes_are_rejected_and_downgraded():
    pages = _jayjagle_pages()
    service = Service(
        name="Artificial Intelligence Consulting",
        description="Developing proprietary LLM architectures and neural pipelines.",
        target_industry="Tech",
        target_customer="CTOs",
        main_benefit="Cutting edge AI",
        source="website",
        evidence=[
            Evidence(
                quote="We develop proprietary LLM architectures and neural pipelines",
                source_url="https://jayjagleinnovations.com",
            )
        ],
    )
    grounded = ground_service(service, pages)
    assert grounded.source == "inferred"
    assert grounded.provenance == "inferred"
    assert grounded.description_provenance == "inferred"
    assert grounded.evidence == []


# =============================================================================
# 4. Quotes attributed to the wrong page are rejected
# =============================================================================
def test_cross_page_misattribution_rejected():
    pages = _jayjagle_pages()
    # Contact address is on /contact, but LLM attributes it to homepage /
    ev_wrong_page = Evidence(
        quote="G9 Business Centre, Karadva Road, Dindoli, Surat",
        source_url="https://www.jayjagleinnovations.com/",
    )
    assert validate_evidence(ev_wrong_page, pages) is False

    # When attributed to correct /contact page, it must pass
    ev_correct_page = Evidence(
        quote="G9 Business Centre, Karadva Road, Dindoli, Surat",
        source_url="https://www.jayjagleinnovations.com/contact",
    )
    assert validate_evidence(ev_correct_page, pages) is True


# =============================================================================
# 5. Service supported by body paragraph has verified offering and description
# =============================================================================
def test_service_supported_by_paragraph_verifies_description():
    page = PageContent(
        url="https://example.com/services",
        title="Our Services | Example",
        blocks=[
            "# Cloud Infrastructure Management",
            "We provide 24/7 proactive monitoring, automated backups, and instant incident response for AWS and GCP.",
        ],
    )
    service = Service(
        name="Cloud Infrastructure Management",
        description="We provide 24/7 proactive monitoring, automated backups, and instant incident response for AWS and GCP.",
        target_industry="Cloud / DevOps",
        target_customer="Engineering Leaders",
        main_benefit="Maximum uptime",
        source="website",
        evidence=[
            Evidence(
                quote="We provide 24/7 proactive monitoring, automated backups, and instant incident response",
                source_url="https://example.com/services",
            )
        ],
    )
    grounded = ground_service(service, [page])
    assert grounded.source == "website"
    assert grounded.provenance == "website_verified"
    assert grounded.description_provenance == "website_verified"


# =============================================================================
# 6. Service with elaborated description has verified offering but inferred desc
# =============================================================================
def test_service_with_elaborated_description_has_inferred_desc_provenance():
    pages = _jayjagle_pages()
    service = Service(
        name="Brand Trademark Registration",
        description="Comprehensive trademark filing, class categorization, and government liaison in India.",
        target_industry="Startups and Businesses",
        target_customer="Brand Owners",
        main_benefit="Legal protection of intellectual property",
        source="website",
        evidence=[
            Evidence(
                quote="Brand Trademark Registration",
                source_url="https://jayjagleinnovations.com",
            )
        ],
    )
    grounded = ground_service(service, pages)
    assert grounded.source == "website"
    assert grounded.provenance == "website_verified"
    assert grounded.description_provenance == "inferred"


# =============================================================================
# 7. Brand verified from title/OG metadata sets name_provenance="website_verified"
# =============================================================================
def test_brand_verified_from_title_and_og():
    pages = _jayjagle_pages()
    assert is_brand_name_verified("Jay Jagle Innovations", pages) is True
    assert is_brand_name_verified("Nonexistent Brand Corp", pages) is False

    brand = Brand(
        name="Jay Jagle Innovations",
        category="Digital Marketing",
        summary="Digital marketing and branding agency in Surat.",
        evidence=[
            Evidence(
                quote="Printing Services(Visiting Card, Letterheads, Envelopes)to help you establish your brand",
                source_url="https://www.jayjagleinnovations.com/",
            )
        ],
    )
    grounded = ground_brand(brand, pages)
    assert grounded.name_provenance == "website_verified"
    assert grounded.summary_provenance == "website_verified"
    assert grounded.provenance == "website_verified"
    assert len(grounded.evidence) == 1


# =============================================================================
# 8. Generic boilerplate quote rejected for brand verification
# =============================================================================
def test_generic_boilerplate_quote_rejected_for_brand_verification():
    pages = _jayjagle_pages()

    # Pure legal/footer boilerplate without brand name
    boilerplate_quote = "All rights reserved. Privacy Policy | Terms of Service"
    assert is_generic_boilerplate_quote(boilerplate_quote, "Jay Jagle Innovations") is True

    brand_with_boilerplate = Brand(
        name="Jay Jagle Innovations",
        category="Digital Marketing",
        summary="Digital marketing agency.",
        evidence=[
            Evidence(
                quote=boilerplate_quote,
                source_url="https://www.jayjagleinnovations.com/",
            )
        ],
    )
    grounded = ground_brand(brand_with_boilerplate, pages)
    # Brand name is verified from metadata/title
    assert grounded.name_provenance == "website_verified"
    # But boilerplate evidence is rejected
    assert grounded.summary_provenance == "inferred"
    assert grounded.provenance == "inferred"
    assert grounded.evidence == []

    # Quote with brand name is NOT rejected
    brand_mention_quote = "© 2026 Jay Jagle Innovations. All rights reserved."
    assert is_generic_boilerplate_quote(brand_mention_quote, "Jay Jagle Innovations") is False


# =============================================================================
# 9. Local Jay Jagle fixture integration test: all 5 services ground successfully
# =============================================================================
def test_jay_jagle_all_services_ground_without_false_negatives():
    pages = _jayjagle_pages()

    # The 5 services identified on Jay Jagle's site
    services = [
        Service(
            name="Social Media Marketing",
            description="Social media strategy and advertising.",
            target_industry="All",
            target_customer="SMBs",
            main_benefit="Brand presence",
            source="website",
            evidence=[Evidence(quote="Social Media Marketing", source_url="https://jayjagleinnovations.com")],
        ),
        Service(
            name="Website Development",
            description="Custom modern web development.",
            target_industry="All",
            target_customer="Businesses",
            main_benefit="Online footprint",
            source="website",
            evidence=[Evidence(quote="Website Development", source_url="https://jayjagleinnovations.com")],
        ),
        Service(
            name="Professional Logo Design",
            description="Vector logo design and identity kits.",
            target_industry="All",
            target_customer="Startups",
            main_benefit="Visual recognition",
            source="website",
            evidence=[Evidence(quote="Professional Logo Design", source_url="https://jayjagleinnovations.com")],
        ),
        Service(
            name="Brand Trademark Registration",
            description="Trademark registration and filing assistance.",
            target_industry="All",
            target_customer="Entrepreneurs",
            main_benefit="Legal safety",
            source="website",
            evidence=[Evidence(quote="Brand Trademark Registration", source_url="https://jayjagleinnovations.com")],
        ),
        Service(
            name="Printing Services",
            description="Visiting cards, letterheads, and envelopes printing.",
            target_industry="All",
            target_customer="Corporate clients",
            main_benefit="High quality physical collateral",
            source="website",
            evidence=[Evidence(quote="High quality printing for your brand", source_url="https://jayjagleinnovations.com")],
        ),
    ]

    grounded_services = [ground_service(s, pages) for s in services]

    # Verify that NONE of the 5 services were falsely downgraded to inferred!
    for s in grounded_services:
        assert s.source == "website", f"Service '{s.name}' was unexpectedly downgraded to {s.source}"
        assert s.provenance == "website_verified", f"Service '{s.name}' provenance was {s.provenance}"
        assert len(s.evidence) == 1, f"Service '{s.name}' evidence was lost"

    # Geographic focus from contact page
    audience = TargetAudience(
        primary_audience=["Business owners"],
        geographic_focus="Surat, Gujarat, India",
        geographic_focus_evidence=[
            Evidence(
                quote="3rd Floor, Shop No. 334/B, G9 Business Centre, Karadva Road, Dindoli, Surat - 394210, Gujarat, India.",
                source_url="https://www.jayjagleinnovations.com/contact",
            )
        ],
    )
    grounded_aud = ground_target_audience(audience, pages)
    assert grounded_aud.geographic_focus_provenance == "website_verified"
    assert len(grounded_aud.geographic_focus_evidence) == 1
