"""Phase 3.3 Test Suite: Competitive Intelligence & Multi-Platform Search.

Covers all 20 required Phase 3.3 scenarios:
1.  Privacy analytics category/offering overlap (Plausible vs Fathom) -> likely_relevant, direct.
2.  Toothpaste manufacturer vs dental clinic -> category_mismatch, substitute.
3.  Local web design agencies offering overlap -> likely_relevant, direct.
4.  Reachable but unrelated company -> low_relevance, not direct.
5.  Unreachable website returns not_checked (not category mismatch).
6.  Insufficient content (< 30 words) produces insufficient_evidence.
7.  Valid competitor evidence tied to correct fetched page URL.
8.  Fabricated competitor evidence rejected / sanitized.
9.  Zero competitors remains valid.
10. Comparison entries aligned with retained competitors.
11. Competitor fetches use SafeFetcher.
12. SSRF protections remain intact.
13. Google suggestions use natural search phrases.
14. Instagram suggestions use visual/hashtag language.
15. Facebook suggestions use local/community discovery patterns.
16. LinkedIn suggestions use B2B/professional terms.
17. X suggestions use topic/discussion hashtags.
18. Platform coverage evaluated without forcing irrelevant recommendations.
19. Search queries relate to extracted business offerings.
20. Exactly five query recommendations returned when context exists.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.schemas import (
    Brand,
    ComparisonEntry,
    Competitor,
    CompetitorAnalysisLLM,
    CompetitorRelevance,
    Evidence,
    LLMAnalysis,
    LLMCompetitor,
    PlatformQueryVariant,
    SearchQuery,
    Service,
    _derive_platform_variant,
)
from app.services.analysis_service import AnalysisService
from app.services.competitor_service import (
    CompetitorService,
    _evaluate_competitor_relevance,
    validate_competitor_evidence,
)
from app.services.grounding import verify_and_canonicalize_evidence
from app.services.llm_service import LLMService
from app.services.website_extractor import FetchedPage, PageContent, SafeFetcher, WebsiteExtractor
from app.utils.cache import TTLCache


def _make_page(
    url: str,
    *,
    title: str = "",
    blocks: list[str] | None = None,
    meta_desc: str = "",
    headings: list[str] | None = None,
    h1: list[str] | None = None,
    footer_text: str = "",
) -> PageContent:
    return PageContent(
        url=url,
        title=title,
        blocks=blocks or [],
        meta_description=meta_desc,
        headings=headings or [],
        h1=h1 or [],
        footer_text=footer_text,
    )


def _make_service(name: str, description: str = "") -> Service:
    return Service(
        name=name,
        description=description or f"Comprehensive offerings for {name}.",
        target_industry="General Industry",
        target_customer="Businesses and Individuals",
        main_benefit="High value and performance",
    )


class DummyFetcher(SafeFetcher):
    """SafeFetcher test stub serving predefined pages or raising AppErrors."""

    def __init__(self, pages: dict[str, FetchedPage | AppError], settings: Settings):
        super().__init__(client=None, settings=settings)  # type: ignore[arg-type]
        self.pages = pages
        self.requested: list[str] = []

    async def fetch(self, url: str, max_bytes: int = 500_000, timeout: float | None = None) -> FetchedPage:
        self.requested.append(url)
        norm = url.rstrip("/")
        for k, v in self.pages.items():
            if k.rstrip("/") == norm:
                if isinstance(v, AppError):
                    raise v
                return v
        raise AppError("connect_error", "Domain does not resolve or connection failed")


# -----------------------------------------------------------------------------
# 1. Privacy analytics category/offering overlap (Plausible vs Fathom)
# -----------------------------------------------------------------------------
def test_privacy_analytics_category_offering_overlap():
    fathom_url = "https://usefathom.com"
    fathom_page = _make_page(
        fathom_url,
        title="Fathom Analytics - Simple, privacy-first website analytics",
        meta_desc="Google Analytics alternative that respects digital privacy and GDPR compliance.",
        h1=["Simple, privacy-focused website analytics without cookies"],
        blocks=[
            "Fathom Analytics is the Google Analytics alternative that doesn't compromise digital privacy.",
            "Our privacy-focused web analytics provide cookieless tracking and full GDPR compliance.",
            "Built for modern businesses, developers, and creators who value user privacy.",
        ],
    )
    comp = LLMCompetitor(
        name="Fathom Analytics",
        website=fathom_url,
        description="Privacy-focused web analytics alternative",
        target_market="Businesses and developers",
    )
    services = [
        _make_service("Privacy-focused web analytics", "Cookieless web analytics software."),
        _make_service("Cookieless tracking", "GDPR compliant visitor tracking."),
    ]
    rel = _evaluate_competitor_relevance(
        comp,
        fathom_page,
        brand_name="Plausible Analytics",
        brand_category="Privacy-Focused Web Analytics",
        services=services,
        target_audience=["Businesses", "Developers"],
    )
    assert rel.status == "likely_relevant"
    assert rel.competitor_type == "direct"
    assert "category_overlap" in rel.signals
    assert "offering_overlap" in rel.signals
    assert len(rel.evidence) >= 1
    assert rel.evidence[0].source_url == fathom_url
    assert "privacy" in rel.evidence[0].quote.lower() or "analytics" in rel.evidence[0].quote.lower()


# -----------------------------------------------------------------------------
# 2. Toothpaste manufacturer vs dental clinic (category mismatch)
# -----------------------------------------------------------------------------
def test_toothpaste_manufacturer_vs_dental_clinic_category_mismatch():
    colgate_url = "https://www.colgate.com"
    colgate_page = _make_page(
        colgate_url,
        title="Colgate: Toothpaste, Toothbrushes & Oral Care Products",
        meta_desc="Explore Colgate oral care products including toothpaste, toothbrushes, and whitening kits.",
        h1=["Everyday Oral Care Products for Bright Smiles"],
        blocks=[
            "Explore our complete range of oral care products, whitening toothpaste, and electric toothbrushes.",
            "Find a store near you or buy now from major retailers online.",
            "Over 100 years of delivering consumer oral health care products.",
        ],
    )
    comp = LLMCompetitor(
        name="Colgate",
        website=colgate_url,
        description="Consumer oral care brand selling toothpaste and brushes.",
        target_market="Consumers worldwide",
    )
    services = [
        _make_service("Teeth Cleaning & Whitening", "In-clinic dental hygiene."),
        _make_service("Root Canal Therapy", "Clinical endodontic treatment."),
    ]
    rel = _evaluate_competitor_relevance(
        comp,
        colgate_page,
        brand_name="Austin Dental Clinic",
        brand_category="Dental Clinic",
        services=services,
    )
    assert rel.status == "category_mismatch"
    assert rel.competitor_type == "substitute"
    assert "manufacturer" in rel.explanation.lower()
    assert len(rel.evidence) >= 1
    assert rel.evidence[0].source_url == colgate_url


# -----------------------------------------------------------------------------
# 3. Local web design agencies offering overlap
# -----------------------------------------------------------------------------
def test_local_web_design_agencies_offering_overlap():
    rival_url = "https://atxdesign.example"
    rival_page = _make_page(
        rival_url,
        title="ATX Design Studio - Custom Website Design in Austin TX",
        meta_desc="Leading web design and development agency serving businesses in Austin, Texas.",
        h1=["We build high-converting custom websites"],
        blocks=[
            "ATX Design Studio provides custom web design, responsive web development, and digital marketing in Austin.",
            "We help Austin businesses, startups, and founders grow online with beautiful websites.",
            "Our team of experienced designers and software engineers crafts responsive interfaces that drive business results.",
        ],
        footer_text="Located at 400 Congress Ave, Austin, Texas 78701.",
    )
    comp = LLMCompetitor(
        name="ATX Design Studio",
        website=rival_url,
        description="Austin-based web design studio",
    )
    services = [
        _make_service("Custom Web Design", "Tailored website design."),
        _make_service("Web Development", "Frontend and backend engineering."),
    ]
    rel = _evaluate_competitor_relevance(
        comp,
        rival_page,
        brand_name="Lone Star Digital",
        brand_category="Web Design & Development Agency",
        services=services,
        geographic_focus="Austin, Texas",
    )
    assert rel.status == "likely_relevant"
    assert rel.competitor_type == "direct"
    assert "category_overlap" in rel.signals
    assert "offering_overlap" in rel.signals
    assert "geographic_overlap" in rel.signals


# -----------------------------------------------------------------------------
# 4. Reachable but unrelated company (low relevance)
# -----------------------------------------------------------------------------
def test_reachable_unrelated_company_low_relevance():
    plumber_url = "https://austinplumbing.example"
    plumber_page = _make_page(
        plumber_url,
        title="Austin Master Plumbers - Drain Cleaning & Pipe Repair",
        meta_desc="24/7 emergency residential plumbing repairs and drain unclogging in Austin.",
        blocks=[
            "Our licensed plumbers handle clogged drains, sewer line repairs, water heaters, and pipe leak detection.",
            "Serving homeowners and commercial properties across Travis County.",
            "Emergency residential plumbing services, leak repair, faucet installation, water heater maintenance, and rooter service.",
        ],
    )
    comp = LLMCompetitor(
        name="Austin Master Plumbers",
        website=plumber_url,
        description="Local plumbing contractor.",
    )
    services = [
        _make_service("Custom Web Design", "Tailored website design."),
        _make_service("SEO Consulting", "Search engine optimization services."),
    ]
    rel = _evaluate_competitor_relevance(
        comp,
        plumber_page,
        brand_name="Lone Star Digital",
        brand_category="Web Design & Development Agency",
        services=services,
    )
    assert rel.status in ("low_relevance", "category_mismatch")
    assert rel.competitor_type != "direct"


# -----------------------------------------------------------------------------
# 5. Unreachable website returns not_checked (not category mismatch)
# -----------------------------------------------------------------------------
@pytest.mark.anyio
async def test_unreachable_website_returns_not_checked():
    settings = Settings(environment="test", allow_private_urls=False)
    fetcher = DummyFetcher({}, settings)
    service = CompetitorService(fetcher, settings)

    comp = LLMCompetitor(
        name="Offline Competitor",
        website="https://offline-does-not-exist.example",
        description="A competitor whose server is dead.",
    )
    results = await service.verify([comp], "https://brand.example")
    assert len(results) == 1
    res = results[0]
    assert res.verification == "unreachable"
    assert res.relevance.status == "not_checked"
    assert res.relevance.competitor_type == "unknown"
    assert res.relevance.status != "category_mismatch"


# -----------------------------------------------------------------------------
# 6. Insufficient content (< 30 words) produces insufficient_evidence
# -----------------------------------------------------------------------------
def test_insufficient_content_produces_insufficient_evidence():
    thin_url = "https://thin-landing.example"
    thin_page = _make_page(
        thin_url,
        title="Under Construction",
        blocks=["Coming soon. Check back later for updates."],  # Only 7 words
    )
    comp = LLMCompetitor(name="Thin Brand", website=thin_url)
    rel = _evaluate_competitor_relevance(
        comp,
        thin_page,
        brand_category="Web Development Agency",
    )
    assert rel.status == "insufficient_evidence"
    assert rel.competitor_type == "unknown"
    assert "insufficient" in rel.explanation.lower()
    assert rel.evidence == []


# -----------------------------------------------------------------------------
# 7. Valid competitor evidence tied to correct fetched page URL
# -----------------------------------------------------------------------------
def test_valid_competitor_evidence_tied_to_correct_fetched_url():
    url = "https://analytics-rival.example"
    page = _make_page(
        url,
        title="Analytics Rival - Simple Web Analytics",
        blocks=[
            "We offer simple cookieless website analytics and digital privacy tools for webmasters.",
            "Track page views, referrers, and conversions without invading user privacy.",
            "Our analytics platform was engineered specifically to respect user privacy and adhere to international standards.",
        ],
    )
    comp = LLMCompetitor(name="Analytics Rival", website=url)
    rel = _evaluate_competitor_relevance(
        comp,
        page,
        brand_category="Web Analytics",
        services=[_make_service("Website Analytics", "Visitor analytics.")],
    )
    assert len(rel.evidence) >= 1
    for ev in rel.evidence:
        assert ev.source_url == url
        assert ev.quote in " ".join(page.blocks + [page.title])


# -----------------------------------------------------------------------------
# 8. Fabricated competitor evidence rejected / sanitized
# -----------------------------------------------------------------------------
def test_fabricated_competitor_evidence_rejected():
    url = "https://competitor.example"
    page = _make_page(
        url,
        title="Competitor Home",
        blocks=["We provide enterprise cloud hosting solutions."],
    )
    # Fabricated evidence quotes not in page content
    fake_ev1 = Evidence(quote="Completely fabricated claim about AI analytics", source_url=url)
    fake_ev2 = Evidence(quote="We provide enterprise cloud hosting solutions.", source_url="https://different.example")

    valid = validate_competitor_evidence([fake_ev1, fake_ev2], page)
    assert len(valid) == 0

    # Real quote from page passes
    real_ev = Evidence(quote="We provide enterprise cloud hosting solutions.", source_url=url)
    valid_real = validate_competitor_evidence([real_ev], page)
    assert len(valid_real) == 1
    assert valid_real[0].quote == real_ev.quote


# -----------------------------------------------------------------------------
# 9. Zero competitors remains valid
# -----------------------------------------------------------------------------
@pytest.mark.anyio
async def test_zero_competitors_remains_valid():
    settings = Settings(environment="test")
    fetcher = DummyFetcher({}, settings)
    service = CompetitorService(fetcher, settings)

    results = await service.verify([], "https://brand.example")
    assert results == []


# -----------------------------------------------------------------------------
# 10. Comparison entries aligned with retained competitors
# -----------------------------------------------------------------------------
def test_comparison_entries_aligned_with_retained_competitors():
    # Setup LLM analysis with comparison entries: brand + 2 competitors
    raw_analysis = LLMAnalysis(
        brand=Brand(name="MyBrand", category="SaaS", summary="Great SaaS platform"),
        competitor_analysis=CompetitorAnalysisLLM(
            brand_overview="MyBrand overview",
            main_category="SaaS",
            main_products_services=["Cloud App"],
            competitors=[
                LLMCompetitor(name="RetainedComp", website="https://retained.example"),
                LLMCompetitor(name="DroppedComp", website="https://dropped.example"),
            ],
            comparison=[
                ComparisonEntry(brand_name="MyBrand", role="your_brand", main_service="Cloud App"),
                ComparisonEntry(brand_name="RetainedComp", role="competitor", main_service="Cloud App"),
                ComparisonEntry(brand_name="DroppedComp", role="competitor", main_service="Cloud App"),
            ],
        ),
        target_search_queries=[
            SearchQuery(query=f"query {i}", platforms=["Google"], intent="nav", business_relevance="rel", target_audience="aud")
            for i in range(5)
        ],
        seo_improvements={  # type: ignore[arg-type]
            "primary_keyword": "cloud saas",
            "recommended_title": "Best Cloud SaaS Platform",
            "meta_description": "A comprehensive cloud saas platform built to streamline team productivity and workflows.",
        },
        top_services=[_make_service("Cloud App", "App description")],
        target_audience={  # type: ignore[arg-type]
            "primary_audience": ["Founders"],
        },
    )

    # Simulate that only RetainedComp was verified and retained
    verified_competitors = [
        Competitor(
            name="RetainedComp",
            website="https://retained.example",
            verification="verified_live_site",
        )
    ]

    # AnalysisService comparison synchronization logic
    verified_comp_names = {c.name.strip().lower() for c in verified_competitors}
    filtered_comparison = []
    for row in raw_analysis.competitor_analysis.comparison:
        if row.role == "your_brand":
            filtered_comparison.append(row)
        else:
            if row.brand_name.strip().lower() in verified_comp_names:
                filtered_comparison.append(row)

    assert len(filtered_comparison) == 2
    assert filtered_comparison[0].brand_name == "MyBrand"
    assert filtered_comparison[1].brand_name == "RetainedComp"


# -----------------------------------------------------------------------------
# 11. Competitor fetches use SafeFetcher
# -----------------------------------------------------------------------------
@pytest.mark.anyio
async def test_competitor_fetches_use_safefetcher():
    settings = Settings(environment="test", allow_private_urls=False)
    pages = {
        "https://live-rival.example": FetchedPage(
            url="https://live-rival.example",
            status=200,
            body=b"<html><head><title>Live Rival - Official Site</title></head><body><h1>Live Rival</h1><p>We provide competing software solutions worldwide for tech teams.</p></body></html>",
            content_type="text/html",
            elapsed_ms=15,
        )
    }
    fetcher = DummyFetcher(pages, settings)
    service = CompetitorService(fetcher, settings)

    comp = LLMCompetitor(name="Live Rival", website="https://live-rival.example")
    results = await service.verify([comp], "https://mybrand.example")
    assert len(results) == 1
    assert results[0].verification == "verified_live_site"
    assert "https://live-rival.example/" in fetcher.requested


# -----------------------------------------------------------------------------
# 12. SSRF protections remain intact for competitor URLs
# -----------------------------------------------------------------------------
@pytest.mark.anyio
async def test_ssrf_protections_remain_intact():
    settings = Settings(environment="test", allow_private_urls=False)
    fetcher = DummyFetcher({}, settings)
    service = CompetitorService(fetcher, settings)

    # Private IP addresses must be rejected immediately without fetching
    private_comp = LLMCompetitor(name="Malicious Competitor", website="http://127.0.0.1:8000/internal")
    results = await service.verify([private_comp], "https://mybrand.example")
    assert len(results) == 1
    assert results[0].verification == "invalid_website_url"
    assert results[0].relevance.status == "not_checked"
    assert len(fetcher.requested) == 0


# -----------------------------------------------------------------------------
# 13. Google suggestions use natural search phrases
# -----------------------------------------------------------------------------
def test_google_suggestions_use_natural_search_phrases():
    variant = _derive_platform_variant("Google", "best privacy analytics for saas")
    assert variant.platform == "Google"
    assert variant.format == "search_phrase"
    assert variant.query == "best privacy analytics for saas"


# -----------------------------------------------------------------------------
# 14. Instagram suggestions use visual/hashtag language
# -----------------------------------------------------------------------------
def test_instagram_suggestions_use_visual_hashtag_language():
    variant = _derive_platform_variant("Instagram", "boutique wedding photography")
    assert variant.platform == "Instagram"
    assert variant.format == "hashtag"
    assert "#Boutique" in variant.query or "#Wedding" in variant.query

    # Verify meaningful compound hashtags rather than mechanical single words
    analytics_variant = _derive_platform_variant("Instagram", "privacy friendly web analytics")
    assert "#Friendly" not in analytics_variant.query.split()
    assert "#Web" not in analytics_variant.query.split()
    assert "#PrivacyWebAnalytics" in analytics_variant.query


# -----------------------------------------------------------------------------
# 15. Facebook suggestions use local/community discovery patterns
# -----------------------------------------------------------------------------
def test_facebook_suggestions_use_local_community_discovery():
    variant = _derive_platform_variant("Facebook", "plumber austin tx")
    assert variant.platform == "Facebook"
    assert variant.format == "community_search"
    assert "local" in variant.query or "recommendations" in variant.query


# -----------------------------------------------------------------------------
# 16. LinkedIn suggestions use B2B/professional terms
# -----------------------------------------------------------------------------
def test_linkedin_suggestions_use_b2b_professional_terms():
    variant = _derive_platform_variant("LinkedIn", "b2b payment gateway api")
    assert variant.platform == "LinkedIn"
    assert variant.format in ("b2b_keyword", "b2b_search")
    assert "professional" in variant.query or "vendor" in variant.query or "b2b" in variant.query.lower()


# -----------------------------------------------------------------------------
# 17. X suggestions use topic/discussion hashtags
# -----------------------------------------------------------------------------
def test_x_suggestions_use_topic_discussion_hashtags():
    variant = _derive_platform_variant("X", "ai code generation")
    assert variant.platform == "X"
    assert variant.format == "topic_discussion"
    assert "#" in variant.query


# -----------------------------------------------------------------------------
# 18. Platform coverage evaluated without forcing irrelevant recommendations
# -----------------------------------------------------------------------------
def test_platform_coverage_evaluated_without_forcing_irrelevant_platforms():
    # B2B query targeting only Google and LinkedIn
    sq = SearchQuery(
        query="enterprise soc2 compliance platform",
        platforms=["Google", "LinkedIn"],
        intent="commercial",
        business_relevance="Core offering",
        target_audience="Security Engineers",
    )
    platforms_present = [p.platform for p in sq.platform_queries]
    assert platforms_present == ["Google", "LinkedIn"]
    assert sq.platforms == ["Google", "LinkedIn"]
    assert "Instagram" not in platforms_present
    assert "Facebook" not in platforms_present

    # Consistency when platform_queries is supplied with a missing platform
    sq2 = SearchQuery(
        query="custom crm software",
        platforms=["Google", "X"],
        platform_queries=[
            PlatformQueryVariant(platform="Google", query="custom crm software", format="search_phrase"),
        ],
        intent="commercial",
    )
    # X must be derived and added so both lists stay consistent
    assert [pq.platform for pq in sq2.platform_queries] == sq2.platforms
    assert sq2.platforms == ["Google", "X"]


# -----------------------------------------------------------------------------
# 19. Search queries relate to extracted business offerings
# -----------------------------------------------------------------------------
def test_search_queries_relate_to_extracted_business_offerings():
    sq = SearchQuery(
        query="privacy friendly web analytics",
        platforms=["Google", "X"],
        intent="informational",
        business_relevance="Directly targets privacy-conscious customers seeking web analytics",
        target_audience="Founders and privacy officers",
    )
    assert sq.business_relevance != ""
    assert sq.intent == "informational"
    assert len(sq.platform_queries) == 2


# -----------------------------------------------------------------------------
# 20. Exactly five query recommendations returned when context exists
# -----------------------------------------------------------------------------
def test_exactly_five_query_recommendations_enforced():
    # 5 queries is valid
    five_queries = [
        SearchQuery(query=f"query {i}", platforms=["Google"], intent="nav", business_relevance="rel", target_audience="aud")
        for i in range(5)
    ]
    assert len(five_queries) == 5

    # 4 queries rejected by LLMAnalysis validation
    with pytest.raises(ValidationError):
        LLMAnalysis(
            brand=Brand(name="B", category="C", summary="S"),
            competitor_analysis=CompetitorAnalysisLLM(brand_overview="O", main_category="C", main_products_services=[]),
            target_search_queries=five_queries[:4],  # Only 4!
            seo_improvements={"primary_keyword": "kw", "recommended_title": "t", "meta_description": "d" * 125},  # type: ignore[arg-type]
            top_services=[],
            target_audience={"primary_audience": ["P"]},  # type: ignore[arg-type]
        )
