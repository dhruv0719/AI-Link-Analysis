from scripts.benchmark import grounded_ratio, pct, summarise, verified_ratio


def test_grounded_ratio():
    services = [{"name": "Custom website development", "source": "website"},
                {"name": "Quantum blockchain consulting", "source": "website"},
                {"name": "Anything", "source": "inferred"}]
    corpus = "we offer custom website development and seo"
    assert grounded_ratio(services, corpus) == 0.5          # inferred services are not counted
    assert grounded_ratio([{"name": "x", "source": "inferred"}], corpus) is None


def test_verified_ratio_and_percentile():
    assert verified_ratio([{"verification": "verified_live_site"}, {"verification": "unreachable"}]) == 0.5
    assert verified_ratio([]) is None
    assert pct([1, 2, 3, 4, 100], 0.5) == 3 and pct([5], 0.95) == 5


def test_summary_table_renders():
    rows = [{"model": "openai:gpt-6-luna", "success": True, "llm_ms": 12000, "input_tokens": 6000, "output_tokens": 3000,
             "cost": 0.0021, "repaired": False, "grounded": 0.8, "verified": 0.75},
            {"model": "openai:gpt-6-luna", "success": False, "error": "llm_unavailable"}]
    table = summarise(rows)
    assert "`openai:gpt-6-luna`" in table and "1/2" in table and "12.0 / 12.0" in table and "80%" in table


def test_evaluator_across_8_categories_fixtures():
    from pathlib import Path
    from scripts.run_benchmark_8_categories import run_offline_evaluation
    from scripts.evaluator import format_evaluation_summary

    fixtures_path = Path("benchmarks/categories_fixtures.json")
    assert fixtures_path.exists()

    results = run_offline_evaluation(fixtures_path)
    assert len(results) == 8

    expected_categories = [
        "SaaS", "E-commerce", "Digital marketing / agency", "Healthcare",
        "EdTech", "Local services / trades", "Developer platform", "Industrial B2B"
    ]
    actual_categories = [r.category for r in results]
    assert set(expected_categories) == set(actual_categories)

    for r in results:
        # Each category in benchmark fixtures should score >= 85%
        assert r.overall_score >= 0.85, f"Category {r.category} scored {r.overall_score}"
        assert r.scores["website_understanding"] >= 0.8
        assert r.scores["grounding_accuracy"] >= 0.8
        assert r.scores["competitor_intelligence"] >= 0.8
        assert r.scores["search_query_quality"] >= 0.8
        assert r.scores["seo_quality"] >= 0.8
        assert r.scores["services_and_audience"] >= 0.8

    summary = format_evaluation_summary(results)
    assert "Executive Summary" not in summary  # format_evaluation_summary returns the table
    assert "| **SaaS** |" in summary
    assert "| **Developer platform** |" in summary


def test_evaluator_flags_quality_defects():
    from scripts.evaluator import evaluate_analysis

    # A payload missing brand, ungrounded services, only 1 platform, bad SEO
    bad_payload = {
        "website": "https://example.com",
        "brand": {"name": "", "category": "", "summary": ""},
        "analysis": {
            "top_services": [
                {"name": "Fake Service", "source": "website", "evidence": []}
            ],
            "competitor_analysis": {
                "competitors": [
                    {"name": "Irrelevant", "relevance": {"status": "unrelated"}}
                ],
                "comparison": [
                    {"brand_name": "Irrelevant", "role": "competitor"}  # Ineligible!
                ]
            },
            "target_search_queries": [
                {"query": "example", "intent": "commercial", "platforms": ["Google"], "platform_queries": []}
            ],
            "seo_improvements": {
                "recommended_title": "Short",  # < 10 chars
                "meta_description": "Too short",  # < 70 chars
                "on_page_checks": []
            },
            "target_audience": {
                "primary_audience": "",
                "customer_needs": [],
                "recommended_marketing_segments": []
            }
        }
    }

    ev = evaluate_analysis(bad_payload, category="SaaS")
    assert ev.overall_score < 0.4
    assert ev.checks["brand_identified"] is False
    assert ev.checks["category_identified"] is False
    assert ev.checks["comparison_eligibility_enforced"] is False
    assert ev.checks["all_five_platforms_covered"] is False
    assert ev.checks["seo_title_bounded"] is False
    assert ev.checks["seo_meta_bounded"] is False
