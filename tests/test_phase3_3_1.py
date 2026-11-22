"""Phase 3.3.1 Test Suite: Final Competitor & Platform Coverage Fixes.

Covers:
1.  All five platforms represented across five queries for a suitable local-service business.
2.  All five platforms represented across five queries for a suitable SaaS business.
3.  Instagram hashtags use meaningful service/category/location terms.
4.  Brand-only hashtags are not used as the default discovery recommendation.
5.  LinkedIn variants are suitable for B2B discovery.
6.  Facebook variants are suitable for local/community discovery.
7.  X variants are suitable for discussion/topic discovery.
8.  likely_relevant and possible candidates can appear in comparison.
9.  category_mismatch candidates are excluded from direct comparisons.
10. insufficient_evidence candidates remain visible as candidates but are excluded from established comparisons.
11. not_checked candidates remain visible but are excluded from established comparisons.
12. The your_brand comparison entry is preserved even when there are zero relevant competitors.
"""
from __future__ import annotations

import pytest

from app.schemas import (
    ComparisonEntry,
    Competitor,
    CompetitorAnalysis,
    CompetitorRelevance,
    PlatformQueryVariant,
    SearchQuery,
    _derive_platform_variant,
)
from app.services.analysis_service import ensure_platform_coverage


# -----------------------------------------------------------------------------
# 1. All five platforms represented across five queries for a local-service business
# -----------------------------------------------------------------------------
def test_all_five_platforms_represented_local_service_business():
    # Model output initially only mentioned Google and Facebook
    raw_queries = [
        SearchQuery(query="custom website design Austin TX", platforms=["Google"], intent="commercial", business_relevance="core web offer", target_audience="Austin businesses"),
        SearchQuery(query="digital marketing agency near me", platforms=["Google", "Facebook"], intent="discovery", business_relevance="local marketing", target_audience="Local business owners"),
        SearchQuery(query="responsive web development company", platforms=["Google"], intent="commercial", business_relevance="technical build", target_audience="Startups"),
        SearchQuery(query="Austin creative studio branding", platforms=["Google"], intent="informational", business_relevance="creative services", target_audience="Brands"),
        SearchQuery(query="local SEO optimization services Austin", platforms=["Google"], intent="transactional", business_relevance="search visibility", target_audience="SMEs"),
    ]

    covered = ensure_platform_coverage(
        raw_queries,
        brand_name="Jay Jagle Innovations",
        brand_category="Digital Marketing & Web Development Agency",
        geographic_focus="Austin, Texas",
    )

    all_platforms = {p for q in covered for p in q.platforms}
    assert all_platforms == {"Google", "Facebook", "Instagram", "LinkedIn", "X"}
    assert len(covered) == 5
    # Verify each query has corresponding platform variants
    for q in covered:
        assert [pq.platform for pq in q.platform_queries] == q.platforms


# -----------------------------------------------------------------------------
# 2. All five platforms represented across five queries for a SaaS business
# -----------------------------------------------------------------------------
def test_all_five_platforms_represented_saas_business():
    # Model output initially only mentioned Google and LinkedIn
    raw_queries = [
        SearchQuery(query="cookieless web analytics software", platforms=["Google", "LinkedIn"], intent="commercial", business_relevance="core SaaS product", target_audience="Product teams"),
        SearchQuery(query="google analytics alternative privacy friendly", platforms=["Google"], intent="comparison", business_relevance="privacy positioning", target_audience="Developers"),
        SearchQuery(query="gdpr compliant website visitor tracking", platforms=["Google", "LinkedIn"], intent="commercial", business_relevance="compliance feature", target_audience="Privacy officers"),
        SearchQuery(query="lightweight web analytics tool", platforms=["Google"], intent="informational", business_relevance="speed & efficiency", target_audience="Site owners"),
        SearchQuery(query="simple privacy analytics dashboard", platforms=["Google"], intent="discovery", business_relevance="UI simplicity", target_audience="Founders"),
    ]

    covered = ensure_platform_coverage(
        raw_queries,
        brand_name="Plausible Analytics",
        brand_category="Privacy-Focused Web Analytics",
    )

    all_platforms = {p for q in covered for p in q.platforms}
    assert all_platforms == {"Google", "Facebook", "Instagram", "LinkedIn", "X"}
    assert len(covered) == 5
    for q in covered:
        assert [pq.platform for pq in q.platform_queries] == q.platforms


# -----------------------------------------------------------------------------
# 3. Instagram hashtags use meaningful service/category/location terms
# -----------------------------------------------------------------------------
def test_instagram_hashtags_use_service_category_location():
    variant = _derive_platform_variant(
        "Instagram",
        "custom website design in Austin TX",
        category="Web Design Agency",
    )
    assert variant.platform == "Instagram"
    assert variant.format == "hashtag"
    # Should contain service, category, or location compounds
    assert "CustomWebsiteDesign" in variant.query or "WebsiteDesign" in variant.query
    assert "#Friendly" not in variant.query.split()
    assert "#In" not in variant.query.split()


# -----------------------------------------------------------------------------
# 4. Brand-only hashtags are not used as the default discovery recommendation
# -----------------------------------------------------------------------------
def test_brand_only_hashtags_avoided_for_discovery():
    # When query has both brand and service tokens
    variant = _derive_platform_variant(
        "Instagram",
        "Plausible web analytics platform",
        brand_name="Plausible",
        category="Web Analytics",
    )
    # The discovery tag should focus on the service/category, not just the brand name alone
    tags = variant.query.split()
    assert "#Plausible" not in tags
    assert any("Analytics" in t or "Web" in t for t in tags)

    # When query is only the brand name, it incorporates the category
    brand_only_variant = _derive_platform_variant(
        "Instagram",
        "Plausible",
        brand_name="Plausible",
        category="Web Analytics",
    )
    assert brand_only_variant.query != "#Plausible"
    assert "WebAnalytics" in brand_only_variant.query or "Analytics" in brand_only_variant.query


# -----------------------------------------------------------------------------
# 5. LinkedIn variants are suitable for B2B discovery
# -----------------------------------------------------------------------------
def test_linkedin_variants_suitable_for_b2b_discovery():
    variant = _derive_platform_variant("LinkedIn", "enterprise analytics compliance")
    assert variant.platform == "LinkedIn"
    assert variant.format == "b2b_keyword"
    assert any(term in variant.query.lower() for term in ("vendor", "b2b", "professional", "enterprise"))


# -----------------------------------------------------------------------------
# 6. Facebook variants are suitable for local/community discovery
# -----------------------------------------------------------------------------
def test_facebook_variants_suitable_for_local_community_discovery():
    variant = _derive_platform_variant("Facebook", "dentist Austin Texas")
    assert variant.platform == "Facebook"
    assert variant.format == "community_search"
    assert any(term in variant.query.lower() for term in ("recommendation", "recommendations", "local"))


# -----------------------------------------------------------------------------
# 7. X variants are suitable for discussion/topic discovery
# -----------------------------------------------------------------------------
def test_x_variants_suitable_for_topic_discovery():
    variant = _derive_platform_variant("X", "cookieless web tracking")
    assert variant.platform == "X"
    assert variant.format == "topic_discussion"
    assert "#" in variant.query
    assert "cookieless" in variant.query.lower()


# -----------------------------------------------------------------------------
# 8. likely_relevant and possible candidates can appear in comparison
# -----------------------------------------------------------------------------
def test_likely_relevant_and_possible_candidates_appear_in_comparison():
    comp_analysis = CompetitorAnalysis(
        brand_overview="Overview",
        main_category="Web Analytics",
        competitors=[
            Competitor(
                name="Matomo",
                website="https://matomo.org",
                verification="verified_live_site",
                relevance=CompetitorRelevance(status="likely_relevant", competitor_type="direct"),
            ),
            Competitor(
                name="Fathom",
                website="https://usefathom.com",
                verification="verified_live_site",
                relevance=CompetitorRelevance(status="possible", competitor_type="indirect"),
            ),
        ],
        comparison=[
            ComparisonEntry(brand_name="Plausible", role="your_brand", main_service="Privacy Analytics"),
            ComparisonEntry(brand_name="Matomo", role="competitor", main_service="Analytics"),
            ComparisonEntry(brand_name="Fathom", role="competitor", main_service="Analytics"),
        ],
    )
    names_in_comparison = [c.brand_name for c in comp_analysis.comparison]
    assert "Plausible" in names_in_comparison
    assert "Matomo" in names_in_comparison
    assert "Fathom" in names_in_comparison
    assert len(comp_analysis.comparison) == 3


# -----------------------------------------------------------------------------
# 9. category_mismatch candidates are excluded from direct comparisons
# -----------------------------------------------------------------------------
def test_category_mismatch_candidates_excluded_from_comparison():
    comp_analysis = CompetitorAnalysis(
        brand_overview="Overview",
        main_category="Dental Clinic",
        competitors=[
            Competitor(
                name="Colgate",
                website="https://www.colgate.com",
                verification="verified_live_site",
                relevance=CompetitorRelevance(status="category_mismatch", competitor_type="substitute"),
            ),
        ],
        comparison=[
            ComparisonEntry(brand_name="Downtown Dental", role="your_brand", main_service="Dental Care"),
            ComparisonEntry(brand_name="Colgate", role="competitor", main_service="Toothpaste"),
        ],
    )
    # Colgate must be excluded from comparison table
    names_in_comparison = [c.brand_name for c in comp_analysis.comparison]
    assert names_in_comparison == ["Downtown Dental"]
    # But Colgate remains visible in the candidate competitor list
    assert len(comp_analysis.competitors) == 1
    assert comp_analysis.competitors[0].name == "Colgate"
    assert comp_analysis.competitors[0].relevance.status == "category_mismatch"


# -----------------------------------------------------------------------------
# 10. insufficient_evidence candidates excluded from comparisons
# -----------------------------------------------------------------------------
def test_insufficient_evidence_candidates_excluded_from_comparison():
    comp_analysis = CompetitorAnalysis(
        brand_overview="Overview",
        main_category="Web Analytics",
        competitors=[
            Competitor(
                name="Google Analytics",
                website="https://analytics.google.com",
                verification="live_site_name_not_matched",
                relevance=CompetitorRelevance(status="insufficient_evidence", competitor_type="unknown"),
            ),
        ],
        comparison=[
            ComparisonEntry(brand_name="Plausible", role="your_brand", main_service="Privacy Analytics"),
            ComparisonEntry(brand_name="Google Analytics", role="competitor", main_service="Analytics"),
        ],
    )
    names_in_comparison = [c.brand_name for c in comp_analysis.comparison]
    assert names_in_comparison == ["Plausible"]
    # Remains in competitors
    assert len(comp_analysis.competitors) == 1
    assert comp_analysis.competitors[0].name == "Google Analytics"
    assert comp_analysis.competitors[0].relevance.status == "insufficient_evidence"


# -----------------------------------------------------------------------------
# 11. not_checked candidates excluded from comparisons
# -----------------------------------------------------------------------------
def test_not_checked_candidates_excluded_from_comparison():
    comp_analysis = CompetitorAnalysis(
        brand_overview="Overview",
        main_category="Web Analytics",
        competitors=[
            Competitor(
                name="Cloudflare Analytics",
                website="https://cloudflare-analytics-down.example",
                verification="unreachable",
                relevance=CompetitorRelevance(status="not_checked", competitor_type="unknown"),
            ),
        ],
        comparison=[
            ComparisonEntry(brand_name="Plausible", role="your_brand", main_service="Privacy Analytics"),
            ComparisonEntry(brand_name="Cloudflare Analytics", role="competitor", main_service="Analytics"),
        ],
    )
    names_in_comparison = [c.brand_name for c in comp_analysis.comparison]
    assert names_in_comparison == ["Plausible"]
    assert len(comp_analysis.competitors) == 1
    assert comp_analysis.competitors[0].relevance.status == "not_checked"


# -----------------------------------------------------------------------------
# 12. The your_brand comparison entry is preserved when zero relevant competitors
# -----------------------------------------------------------------------------
def test_your_brand_comparison_preserved_with_zero_competitors():
    comp_analysis = CompetitorAnalysis(
        brand_overview="Overview",
        main_category="Web Studio",
        competitors=[],
        comparison=[
            ComparisonEntry(brand_name="My Agency", role="your_brand", main_service="Web Design"),
        ],
    )
    assert len(comp_analysis.comparison) == 1
    assert comp_analysis.comparison[0].brand_name == "My Agency"
    assert comp_analysis.comparison[0].role == "your_brand"
