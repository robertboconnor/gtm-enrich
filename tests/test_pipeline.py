"""Pipeline: caching, fallback on model failure, ordering, and partial failure."""

from __future__ import annotations

from gtm_enrich.analyze.llm import AnalysisError
from gtm_enrich.destinations.dryrun import DryRunDestination
from gtm_enrich.models import EnrichmentResult
from gtm_enrich.pipeline import analyze_page, enrich_domains, write_results
from gtm_enrich.scrape.fetch import FetchError


def test_analyze_page_uses_heuristics_when_llm_is_off(sample_page, icp, settings) -> None:
    result = analyze_page(sample_page, icp, settings, use_llm=False)
    assert result.ok
    assert result.provenance.analyzer == "heuristic:v1"
    assert result.page_signals["has_pricing_page"] is True
    assert "HubSpot" in result.tech_signals


def test_analysis_is_cached_by_content_hash(sample_page, icp, settings, monkeypatch) -> None:
    calls = {"n": 0}
    import gtm_enrich.pipeline as pipeline

    real = pipeline.analyze_with_heuristics

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "analyze_with_heuristics", counting)

    analyze_page(sample_page, icp, settings, use_llm=False)
    analyze_page(sample_page, icp, settings, use_llm=False)
    assert calls["n"] == 1


def test_editing_the_icp_invalidates_the_analysis_cache(
    sample_page, icp, settings, monkeypatch
) -> None:
    calls = {"n": 0}
    import gtm_enrich.pipeline as pipeline

    real = pipeline.analyze_with_heuristics
    monkeypatch.setattr(
        pipeline,
        "analyze_with_heuristics",
        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1],
    )

    analyze_page(sample_page, icp, settings, use_llm=False)
    changed = icp.__class__(
        name=icp.name,
        description=icp.description + " Now targeting enterprise only.",
        good_fit=icp.good_fit,
        poor_fit=icp.poor_fit,
    )
    analyze_page(sample_page, changed, settings, use_llm=False)
    assert calls["n"] == 2  # same page, different question


def test_llm_failure_falls_back_to_heuristics(sample_page, icp, settings, monkeypatch) -> None:
    import gtm_enrich.pipeline as pipeline

    def boom(*args, **kwargs):
        raise AnalysisError("API is down")

    monkeypatch.setattr(pipeline, "analyze_with_llm", boom)

    result = analyze_page(sample_page, icp, settings, use_llm=True, provider=object())
    assert result.ok
    assert result.provenance.analyzer == "heuristic:v1"


def test_llm_failure_falls_back_for_every_provider(sample_page, icp, settings, monkeypatch):
    """A dead vendor degrades to heuristics rather than dropping the record."""
    import gtm_enrich.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "analyze_with_llm",
        lambda *a, **k: (_ for _ in ()).throw(AnalysisError("429 rate limited")),
    )
    for provider_name in ("anthropic", "openai"):
        object.__setattr__(settings.analyze, "provider", provider_name)
        result = analyze_page(
            sample_page, icp, settings, use_llm=True, provider=object(), use_cache=False
        )
        assert result.ok
        assert result.provenance.analyzer == "heuristic:v1"


async def test_enrich_domains_preserves_input_order_and_partial_failures(
    sample_page, icp, settings, monkeypatch
) -> None:
    import gtm_enrich.pipeline as pipeline

    async def fake_scrape(domains, scrape_settings, use_cache=True):
        out = {}
        for d in domains:
            if d == "broken.example":
                out[d] = FetchError("connection refused")
            else:
                page = sample_page.model_copy(update={"domain": d})
                out[d] = page
        return out

    monkeypatch.setattr(pipeline, "scrape_many", fake_scrape)

    results = await enrich_domains(
        ["https://acme.example/", "broken.example", "not-a-domain", "third.example"],
        settings,
        icp,
        use_llm=False,
    )

    assert [r.domain for r in results] == [
        "acme.example",
        "broken.example",
        "not-a-domain",
        "third.example",
    ]
    assert results[0].ok
    assert not results[1].ok and "connection refused" in results[1].error
    assert not results[2].ok and "Not a domain" in results[2].error
    assert results[3].ok


async def test_enrich_domains_with_no_valid_domains_returns_only_errors(
    icp, settings
) -> None:
    results = await enrich_domains(["", "nope"], settings, icp, use_llm=False)
    assert len(results) == 2
    assert all(not r.ok for r in results)


def test_write_results_marks_failed_enrichments_without_calling_the_destination(
    mapping, tmp_path
) -> None:
    failed = EnrichmentResult(domain="broken.example", ok=False, error="timeout")
    dest = DryRunDestination(output_dir=tmp_path, shape="hubspot")
    writes = write_results([failed], mapping, dest, "hubspot")
    assert writes[0].action == "failed"
    assert writes[0].error == "timeout"
    assert dest.flush()["json"].read_text().strip() == "[]"


def test_write_results_maps_and_writes_good_records(enrichment, mapping, tmp_path) -> None:
    dest = DryRunDestination(output_dir=tmp_path, shape="salesforce")
    writes = write_results([enrichment], mapping, dest, "salesforce")
    assert writes[0].action == "would_create"
    assert "GTM_ICP_Fit_Score__c" in writes[0].changed_fields


async def test_duplicate_domains_are_scraped_once(sample_page, icp, settings, monkeypatch) -> None:
    import gtm_enrich.pipeline as pipeline

    scraped: list[list[str]] = []

    async def fake_scrape(domains, scrape_settings, use_cache=True):
        scraped.append(list(domains))
        return {d: sample_page.model_copy(update={"domain": d}) for d in domains}

    monkeypatch.setattr(pipeline, "scrape_many", fake_scrape)

    results = await enrich_domains(
        ["acme.example", "https://www.acme.example/pricing", "acme.example"],
        settings,
        icp,
        use_llm=False,
    )
    assert scraped == [["acme.example"]]
    assert len(results) == 1
