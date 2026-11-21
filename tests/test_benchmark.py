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
