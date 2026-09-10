"""Orchestration: domains in, enrichment out, then enrichment into a destination.

Scraping is I/O-bound and runs on asyncio. Analysis is a blocking SDK call and
runs on a small thread pool. Writes are sequential on purpose -- CRM APIs are
rate-limited and a write storm is how you get an org throttled.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .analyze.heuristics import analyze_with_heuristics
from .analyze.llm import AnalysisError, analyze_with_llm, get_provider
from .analyze.prompt import build_system
from .analyze.providers import default_model_for
from .config import IcpProfile, MappingConfig, Settings
from .destinations import Destination
from .mapping import MappingError, build_record
from .models import EnrichmentResult, HomepageAnalysis, Provenance, ScrapedPage, WriteResult
from .scrape.fetch import FetchError, normalize_domain, scrape_many
from .scrape.markdown import detect_page_signals

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, str], None]


def _noop(domain: str, status: str) -> None:
    pass


# --------------------------------------------------------------------------- #
# analysis cache
# --------------------------------------------------------------------------- #


def _analysis_cache_path(settings: Settings, domain: str, key: str) -> Path:
    return settings.scrape.cache_dir.parent / "analysis" / f"{domain}-{key}.json"


def _cache_key(page: ScrapedPage, analyzer: str, icp: IcpProfile) -> str:
    """Analysis is cached against page content *and* the ICP that judged it.

    Edit config/icp.yaml and every cached score is invalidated, which is the
    correct behaviour -- the same page under a new ICP is a different question.
    """
    icp_fingerprint = hashlib.sha256(icp.as_prompt_block().encode()).hexdigest()[:8]
    return f"{page.content_hash}-{analyzer}-{icp_fingerprint}"


def _read_analysis_cache(
    settings: Settings, page: ScrapedPage, key: str
) -> tuple[HomepageAnalysis, Provenance] | None:
    path = _analysis_cache_path(settings, page.domain, key)
    if not path.is_file():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        return (
            HomepageAnalysis.model_validate(blob["analysis"]),
            Provenance.model_validate(blob["provenance"]),
        )
    except (ValueError, KeyError, OSError) as exc:
        log.debug("Ignoring unreadable analysis cache %s: %s", path, exc)
        return None


def _write_analysis_cache(
    settings: Settings, page: ScrapedPage, key: str, analysis: HomepageAnalysis, prov: Provenance
) -> None:
    path = _analysis_cache_path(settings, page.domain, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"analysis": analysis.model_dump(), "provenance": prov.model_dump()},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# enrichment
# --------------------------------------------------------------------------- #


def analyze_page(
    page: ScrapedPage,
    icp: IcpProfile,
    settings: Settings,
    *,
    use_llm: bool,
    provider=None,
    use_cache: bool = True,
) -> EnrichmentResult:
    """Analyze one already-scraped page. Falls back to heuristics on model failure."""
    page_signals = detect_page_signals(page.links, page.markdown)
    if use_llm:
        # The cache key is built from configuration, not from the live provider
        # object -- the settings are what decide which model runs, and this keeps
        # the key computable without instantiating an SDK client.
        model = settings.analyze.model or default_model_for(settings.analyze.provider)
        analyzer = f"{settings.analyze.provider}:{model}"
    else:
        analyzer = "heuristic:v1"
    key = _cache_key(page, analyzer, icp)

    if use_cache:
        cached = _read_analysis_cache(settings, page, key)
        if cached is not None:
            analysis, provenance = cached
            return EnrichmentResult(
                domain=page.domain,
                ok=True,
                analysis=analysis,
                provenance=provenance,
                tech_signals=page.tech_signals,
                page_signals=page_signals,
                from_cache=True,
            )

    if use_llm:
        try:
            analysis, provenance = analyze_with_llm(
                page, page_signals, build_system(icp), settings.analyze, provider=provider
            )
        except AnalysisError as exc:
            log.warning(
                "%s: LLM analysis failed (%s); falling back to heuristics", page.domain, exc
            )
            analysis, provenance = analyze_with_heuristics(page, page_signals, icp)
            key = _cache_key(page, "heuristic:v1", icp)
    else:
        analysis, provenance = analyze_with_heuristics(page, page_signals, icp)

    if use_cache:
        _write_analysis_cache(settings, page, key, analysis, provenance)

    return EnrichmentResult(
        domain=page.domain,
        ok=True,
        analysis=analysis,
        provenance=provenance,
        tech_signals=page.tech_signals,
        page_signals=page_signals,
    )


async def enrich_domains(
    raw_domains: list[str],
    settings: Settings,
    icp: IcpProfile,
    *,
    use_llm: bool,
    use_cache: bool = True,
    progress: ProgressFn = _noop,
) -> list[EnrichmentResult]:
    """Scrape and analyze a list of domains. Never raises for a single bad domain."""
    results: list[EnrichmentResult] = []
    domains: list[str] = []
    # Keyed by the domain that ends up on the result -- normalized when the input
    # parsed, raw when it didn't -- so the final sort can find every row.
    order: dict[str, int] = {}

    for index, raw in enumerate(raw_domains):
        try:
            normalized = normalize_domain(raw)
        except ValueError as exc:
            results.append(EnrichmentResult(domain=raw, ok=False, error=str(exc)))
            order.setdefault(raw, index)
            progress(raw, "invalid")
            continue
        order.setdefault(normalized, index)
        if normalized not in domains:
            domains.append(normalized)

    if not domains:
        return results

    progress("", f"scraping {len(domains)} domains")
    scraped = await scrape_many(domains, settings.scrape, use_cache=use_cache)

    pages: list[ScrapedPage] = []
    for domain in domains:
        outcome = scraped[domain]
        if isinstance(outcome, FetchError):
            results.append(EnrichmentResult(domain=domain, ok=False, error=str(outcome)))
            progress(domain, "fetch failed")
        else:
            pages.append(outcome)
            progress(domain, "cached" if outcome.from_cache else "scraped")

    if not pages:
        return results

    # One provider shared across threads: both SDKs are thread-safe, and a shared
    # client keeps the cached system prompt warm across the batch.
    provider = get_provider(settings.analyze) if use_llm else None

    def run(page: ScrapedPage) -> EnrichmentResult:
        try:
            result = analyze_page(
                page, icp, settings, use_llm=use_llm, provider=provider, use_cache=use_cache
            )
        except Exception as exc:  # noqa: BLE001 - one page must not sink the batch
            log.exception("analysis crashed for %s", page.domain)
            result = EnrichmentResult(domain=page.domain, ok=False, error=str(exc))
        progress(page.domain, "analyzed" if result.ok else "analysis failed")
        return result

    workers = max(1, min(settings.scrape.concurrency, len(pages)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results.extend(pool.map(run, pages))

    results.sort(key=lambda r: order.get(r.domain, len(order)))
    return results


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #


def write_results(
    results: list[EnrichmentResult],
    mapping: MappingConfig,
    destination: Destination,
    shape: str,
    *,
    progress: ProgressFn = _noop,
) -> list[WriteResult]:
    """Map and upsert each successful enrichment. Sequential, by design."""
    writes: list[WriteResult] = []
    for result in results:
        if not result.ok:
            writes.append(
                WriteResult(
                    domain=result.domain,
                    destination=destination.name,
                    action="failed",
                    error=result.error or "enrichment failed",
                )
            )
            continue
        try:
            record = build_record(result, mapping, shape)
        except MappingError as exc:
            writes.append(
                WriteResult(
                    domain=result.domain,
                    destination=destination.name,
                    action="failed",
                    error=f"mapping: {exc}",
                )
            )
            continue
        outcome = destination.upsert(result, record)
        progress(result.domain, outcome.action)
        writes.append(outcome)
    return writes
