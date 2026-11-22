"""Tests for Phase 3.1: Schema Flexibility & Website Extraction Improvements.

Covers:
1. Flexible service cardinality (0, 1, 2, 3, 5 accepted; >5 rejected; no filler required).
2. Honest competitor cardinality (0, 1, 3, 5 accepted; >5 rejected; empty list does not break comparison).
3. Selective footer extraction (business name, address, city/country, phone, email preserved; clutter dropped).
4. Nested URL selection (deep service/product paths selected; pagination/archive/crawl traps excluded).
"""
import copy
import json
import pytest
from pydantic import ValidationError

from app.schemas import (
    ComparisonEntry,
    CompetitorAnalysis,
    CompetitorAnalysisLLM,
    LLMAnalysis,
    LLMCompetitor,
    Service,
)
from app.services.website_extractor import (
    PageContent,
    _extract_footer_context,
    build_prompt_text,
    parse_page,
    pick_key_pages,
)
from tests.conftest import LLM_OUTPUT


# =============================================================================
# 1. Flexible Service Cardinality
# =============================================================================

def _make_service(name: str = "Web Design", source: str = "website") -> dict:
    return {
        "name": name,
        "description": f"Professional {name} for businesses.",
        "target_industry": "SMBs",
        "target_customer": "Founders",
        "main_benefit": "Higher conversions",
        "source": source,
    }


def test_service_cardinality_accepts_one_genuine_service():
    data = copy.deepcopy(LLM_OUTPUT)
    data["top_services"] = [_make_service("Core SaaS Platform", "website")]
    parsed = LLMAnalysis.model_validate(data)
    assert len(parsed.top_services) == 1
    assert parsed.top_services[0].name == "Core SaaS Platform"
    assert parsed.top_services[0].source == "website"


def test_service_cardinality_accepts_two_genuine_services():
    data = copy.deepcopy(LLM_OUTPUT)
    data["top_services"] = [
        _make_service("Cloud Hosting", "website"),
        _make_service("Domain Registration", "website"),
    ]
    parsed = LLMAnalysis.model_validate(data)
    assert len(parsed.top_services) == 2


def test_service_cardinality_accepts_three_genuine_services():
    data = copy.deepcopy(LLM_OUTPUT)
    data["top_services"] = [
        _make_service("Brand Identity", "website"),
        _make_service("Web Design", "website"),
        _make_service("SEO Retainer", "inferred"),
    ]
    parsed = LLMAnalysis.model_validate(data)
    assert len(parsed.top_services) == 3
    assert parsed.top_services[2].source == "inferred"


def test_service_cardinality_accepts_five_genuine_services():
    data = copy.deepcopy(LLM_OUTPUT)
    data["top_services"] = [_make_service(f"Service {i}") for i in range(1, 6)]
    parsed = LLMAnalysis.model_validate(data)
    assert len(parsed.top_services) == 5


def test_service_cardinality_accepts_zero_services():
    data = copy.deepcopy(LLM_OUTPUT)
    data["top_services"] = []
    parsed = LLMAnalysis.model_validate(data)
    assert len(parsed.top_services) == 0


def test_service_cardinality_rejects_more_than_five():
    data = copy.deepcopy(LLM_OUTPUT)
    data["top_services"] = [_make_service(f"Service {i}") for i in range(1, 7)]
    with pytest.raises(ValidationError) as exc_info:
        LLMAnalysis.model_validate(data)
    assert "top_services cannot contain more than 5 items" in str(exc_info.value)


# =============================================================================
# 2. Honest Competitor Cardinality
# =============================================================================

def _make_competitor(name: str, domain: str = "example.com") -> dict:
    return {
        "name": name,
        "website": f"https://{domain}",
        "description": f"{name} company overview",
        "target_market": "Global",
        "strengths": ["Scale"],
        "weaknesses": ["Price"],
    }


def test_competitor_cardinality_accepts_zero_competitors():
    data = copy.deepcopy(LLM_OUTPUT)
    data["competitor_analysis"]["competitors"] = []
    data["competitor_analysis"]["comparison"] = [
        {
            "brand_name": "MyBrand",
            "role": "your_brand",
            "main_service": "Software",
            "target_market": "B2B",
            "key_strength": "Speed",
            "opportunity": "Growth",
        }
    ]
    parsed = LLMAnalysis.model_validate(data)
    assert len(parsed.competitor_analysis.competitors) == 0
    assert len(parsed.competitor_analysis.comparison) == 1
    assert parsed.competitor_analysis.comparison[0].role == "your_brand"


def test_competitor_cardinality_accepts_one_competitor():
    data = copy.deepcopy(LLM_OUTPUT)
    data["competitor_analysis"]["competitors"] = [_make_competitor("DirectRival", "directrival.com")]
    parsed = LLMAnalysis.model_validate(data)
    assert len(parsed.competitor_analysis.competitors) == 1
    assert parsed.competitor_analysis.competitors[0].name == "DirectRival"


def test_competitor_cardinality_accepts_three_competitors():
    data = copy.deepcopy(LLM_OUTPUT)
    data["competitor_analysis"]["competitors"] = [
        _make_competitor("CompA", "compa.com"),
        _make_competitor("CompB", "compb.com"),
        _make_competitor("CompC", "compc.com"),
    ]
    parsed = LLMAnalysis.model_validate(data)
    assert len(parsed.competitor_analysis.competitors) == 3


def test_competitor_cardinality_accepts_five_competitors():
    data = copy.deepcopy(LLM_OUTPUT)
    data["competitor_analysis"]["competitors"] = [_make_competitor(f"Comp{i}", f"comp{i}.com") for i in range(1, 6)]
    parsed = LLMAnalysis.model_validate(data)
    assert len(parsed.competitor_analysis.competitors) == 5


def test_competitor_cardinality_rejects_more_than_five():
    data = copy.deepcopy(LLM_OUTPUT)
    data["competitor_analysis"]["competitors"] = [_make_competitor(f"Comp{i}", f"comp{i}.com") for i in range(1, 7)]
    with pytest.raises(ValidationError) as exc_info:
        LLMAnalysis.model_validate(data)
    assert "competitors cannot contain more than 5 items" in str(exc_info.value)


def test_empty_competitor_list_filters_ghost_comparison_rows():
    comp_data = {
        "brand_overview": "Brand overview text",
        "main_category": "Analytics",
        "competitors": [],
        "comparison": [
            {
                "brand_name": "OurBrand",
                "role": "your_brand",
                "main_service": "Analytics",
                "target_market": "Developers",
                "key_strength": "Privacy",
                "opportunity": "Scale",
            },
            {
                "brand_name": "GhostCompetitor",
                "role": "competitor",
                "main_service": "Big Analytics",
                "target_market": "Enterprise",
                "key_strength": "Features",
                "opportunity": "Simplicity",
            },
        ],
    }
    model = CompetitorAnalysisLLM.model_validate(comp_data)
    # The ghost competitor comparison row must be automatically removed
    assert len(model.comparison) == 1
    assert model.comparison[0].brand_name == "OurBrand"
    assert model.comparison[0].role == "your_brand"


# =============================================================================
# 3. Selective Footer Extraction
# =============================================================================

CONTROLLED_FOOTER_HTML = b"""<!doctype html>
<html>
<head><title>Apex Engineering | Precision Machining</title></head>
<body>
<nav><a href="/services">Services</a><a href="/about">About</a></nav>
<main>
  <h1>Precision CNC Machining Solutions</h1>
  <p>Apex Engineering delivers high-tolerance CNC milling and turning for aerospace applications.</p>
</main>
<footer>
  <div class="nav-links">
    <a href="/privacy">Privacy Policy</a>
    <a href="/terms">Terms of Service</a>
    <a href="/sitemap">Sitemap</a>
    <a href="/careers">Careers</a>
  </div>
  <div class="company-info">
    <p class="legal-name">Apex Engineering Services Ltd.</p>
    <address>456 Silicon Way, Building B, Austin, TX 78701, United States</address>
    <p>Telephone: <a href="tel:+15125550144">+1 (512) 555-0144</a></p>
    <p>Inquiries: <a href="mailto:support@apexeng.example">support@apexeng.example</a></p>
    <small>&copy; 2026 Apex Engineering Services Ltd. Reg. No. 12345678. All rights reserved.</small>
  </div>
</footer>
</body>
</html>
"""


def test_selective_footer_extraction_preserves_location_contact_legal():
    page = parse_page(CONTROLLED_FOOTER_HTML, "https://apexeng.example/")

    # 1. Footer text must be captured
    assert page.footer_text is not None
    ft = page.footer_text

    # 2. Useful signals must survive
    assert "Apex Engineering Services Ltd." in ft
    assert "456 Silicon Way" in ft
    assert "Austin, TX 78701" in ft
    assert "United States" in ft
    assert "+15125550144" in ft or "+1 (512) 555-0144" in ft
    assert "support@apexeng.example" in ft
    assert "2026" in ft

    # 3. Navigation clutter and boilerplate links must NOT be retained in footer_text
    assert "Privacy Policy" not in ft
    assert "Terms of Service" not in ft
    assert "Sitemap" not in ft
    assert "Careers" not in ft

    # 4. Body text blocks must remain clean (footer tags decomposed from main text)
    body_text = " ".join(page.blocks)
    assert "Precision CNC Machining Solutions" in body_text
    assert "456 Silicon Way" not in body_text
    assert "support@apexeng.example" not in body_text


def test_build_prompt_text_includes_footer_context_header():
    page = parse_page(CONTROLLED_FOOTER_HTML, "https://apexeng.example/")
    prompt_text = build_prompt_text([page], budget=10000)

    assert "Footer context:" in prompt_text
    assert "Austin, TX 78701" in prompt_text
    assert "support@apexeng.example" in prompt_text


# =============================================================================
# 4. Nested URL Selection
# =============================================================================

def test_pick_key_pages_selects_nested_service_and_product_pages():
    page = PageContent(
        url="https://example.com/",
        internal_links=[
            ("Web Development Services", "https://example.com/en/services/web-development"),
            ("Healthcare Solutions", "https://example.com/solutions/industry/healthcare"),
            ("Analytics Software", "https://example.com/products/software/analytics"),
            ("Contact Us", "https://example.com/contact"),
            ("About Our Team", "https://example.com/about/team"),
            # Pagination / Noise
            ("Page 2", "https://example.com/blog/page/2/"),
            ("Page 3", "https://example.com/p/3"),
            ("2024 Archives", "https://example.com/archives/2024/"),
            ("Date Post", "https://example.com/2024/05/update"),
            ("Tag Page", "https://example.com/tag/design"),
            ("Category Page", "https://example.com/category/news"),
            ("Privacy", "https://example.com/privacy-policy"),
            ("Cart", "https://example.com/cart"),
            ("Checkout", "https://example.com/checkout"),
            ("Login", "https://example.com/login"),
            # Crawl trap (repeating segment)
            ("Trap", "https://example.com/services/web/services/web"),
        ],
    )

    picked = pick_key_pages(page, limit=5)

    # Legitimate nested pages must be picked
    assert "https://example.com/en/services/web-development" in picked
    assert "https://example.com/solutions/industry/healthcare" in picked
    assert "https://example.com/products/software/analytics" in picked

    # Noise, utility, and pagination paths must be excluded
    assert "https://example.com/blog/page/2/" not in picked
    assert "https://example.com/p/3" not in picked
    assert "https://example.com/archives/2024/" not in picked
    assert "https://example.com/2024/05/update" not in picked
    assert "https://example.com/tag/design" not in picked
    assert "https://example.com/category/news" not in picked
    assert "https://example.com/privacy-policy" not in picked
    assert "https://example.com/cart" not in picked
    assert "https://example.com/checkout" not in picked
    assert "https://example.com/login" not in picked
    assert "https://example.com/services/web/services/web" not in picked
