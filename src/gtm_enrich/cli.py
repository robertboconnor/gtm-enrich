"""Command line interface.

Four commands, in the order you would actually use them:

    gtm-enrich check                 # is my config and are my credentials sane?
    gtm-enrich fields --dest hubspot # what do I have to create in the CRM first?
    gtm-enrich scrape wistia.com     # what does the scraper actually see?
    gtm-enrich run --domains ...     # the whole pipeline
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .analyze.providers import PROVIDER_NAMES, default_model_for
from .config import (
    ConfigError,
    IcpProfile,
    MappingConfig,
    Settings,
    has_llm_credentials,
    load_env,
)
from .destinations import DESTINATIONS, DestinationError, build_destination
from .destinations.dryrun import DryRunDestination
from .mapping import mapping_field_names
from .models import EnrichmentResult
from .pipeline import enrich_domains, write_results
from .scrape.fetch import FetchError, RobotsCache, build_client, normalize_domain, scrape_domain
from .scrape.fetchers import SCRAPER_REQUIREMENTS, SCRAPERS, FetcherError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Scrape a homepage, answer GTM questions about it, write the answers to a CRM.",
)
console = Console()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _settings(
    provider: str | None = None, model: str | None = None, scraper: str | None = None
) -> Settings:
    """Build settings from the environment, with CLI flags taking precedence.

    `.env` is loaded first so a local file can supply keys, but it never
    overrides a variable the shell or CI already set.
    """
    load_env()
    settings = Settings.from_env()
    if provider:
        object.__setattr__(settings.analyze, "provider", provider)
    if model:
        object.__setattr__(settings.analyze, "model", model)
    if scraper:
        object.__setattr__(settings.scrape, "backend", scraper)
    return settings


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _load_configs(icp_path: Path, mapping_path: Path) -> tuple[IcpProfile, MappingConfig]:
    try:
        return IcpProfile.load(icp_path), MappingConfig.load(mapping_path)
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def read_domain_file(path: Path) -> list[str]:
    """Read domains from a .csv (a `domain`/`website` column, or the first one) or a .txt."""
    if not path.is_file():
        console.print(f"[red]No such file:[/red] {path}")
        raise typer.Exit(code=2)

    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() != ".csv":
        return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]

    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    for candidate in ("domain", "website", "company_domain", "url"):
        if candidate in header:
            idx = header.index(candidate)
            return [r[idx].strip() for r in rows[1:] if len(r) > idx and r[idx].strip()]
    # No recognizable header: assume the first column, and keep row 1 if it
    # doesn't look like a header.
    start = 0 if "." in rows[0][0] else 1
    return [r[0].strip() for r in rows[start:] if r and r[0].strip()]


def _results_table(results: list[EnrichmentResult]) -> Table:
    table = Table(title="Enrichment", header_style="bold", show_lines=False)
    table.add_column("Domain", style="cyan", no_wrap=True)
    table.add_column("Company")
    table.add_column("Category")
    table.add_column("Segment")
    table.add_column("ICP", justify="right")
    table.add_column("Conf", justify="right")
    table.add_column("Signals", overflow="fold")

    for r in results:
        if not r.ok or r.analysis is None:
            table.add_row(r.domain, "[red]failed[/red]", (r.error or "")[:60], "", "", "", "")
            continue
        a = r.analysis
        score = a.icp_fit_score
        colour = "green" if score >= 70 else "yellow" if score >= 45 else "red"
        table.add_row(
            r.domain,
            a.company_name[:28],
            a.category[:22],
            a.segment,
            f"[{colour}]{score}[/{colour}]",
            f"{a.confidence:.2f}",
            "; ".join(a.buying_signals)[:50] or "—",
        )
    return table


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


@app.command()
def check(
    icp: Path = typer.Option(Path("config/icp.yaml"), help="ICP definition."),
    mapping: Path = typer.Option(Path("config/mapping.yaml"), help="Field mapping."),
) -> None:
    """Validate config and report which credentials are present."""
    env_file = load_env()
    settings = Settings.from_env()
    icp_profile, mapping_config = _load_configs(icp, mapping)

    ok, warn = "[green]ok[/green]", "[yellow]not set[/yellow]"

    table = Table(title="gtm-enrich check", header_style="bold")
    table.add_column("Item")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    table.add_row(
        ".env",
        ok if env_file else "[dim]none[/dim]",
        str(env_file) if env_file else "no .env found — reading the shell environment only",
    )
    table.add_row("ICP config", ok, f"{icp} — {icp_profile.name}")
    table.add_row("Field mapping", ok, f"{mapping} — {len(mapping_config.fields)} fields")
    console.print(table)

    # --- analysis providers ---
    llm_table = Table(title="LLM providers", header_style="bold")
    llm_table.add_column("Provider")
    llm_table.add_column("Status")
    llm_table.add_column("Default model")
    llm_table.add_column("Needs")
    any_llm = False
    for name in PROVIDER_NAMES:
        available = has_llm_credentials(name)
        any_llm = any_llm or available
        marker = " [dim](selected)[/dim]" if name == settings.analyze.provider else ""
        llm_table.add_row(
            f"{name}{marker}",
            ok if available else warn,
            settings.analyze.model or default_model_for(name),
            "ANTHROPIC_API_KEY" if name == "anthropic" else "OPENAI_API_KEY",
        )
    console.print(llm_table)

    # --- scraper backends ---
    scrape_table = Table(title="Scraper backends", header_style="bold")
    scrape_table.add_column("Backend")
    scrape_table.add_column("Status")
    scrape_table.add_column("Renders JS")
    scrape_table.add_column("Needs", overflow="fold")
    checks = {
        "direct": True,
        "firecrawl": bool(os.getenv("FIRECRAWL_API_KEY")),
        "crawl4ai": True,  # a local server; reachability is only knowable at run time
        "apify": bool(os.getenv("APIFY_API_TOKEN")),
    }
    for name in SCRAPERS:
        marker = " [dim](selected)[/dim]" if name == settings.scrape.backend else ""
        status = ok if checks[name] else warn
        if name == "crawl4ai":
            status = "[dim]needs server[/dim]"
        scrape_table.add_row(
            f"{name}{marker}", status, "no" if name == "direct" else "yes",
            SCRAPER_REQUIREMENTS[name],
        )
    console.print(scrape_table)

    # --- destinations ---
    dest_table = Table(title="Destinations", header_style="bold")
    dest_table.add_column("Destination")
    dest_table.add_column("Status")
    dest_table.add_column("Needs", overflow="fold")
    hs = bool(os.getenv("HUBSPOT_PRIVATE_APP_TOKEN"))
    sf = bool(os.getenv("SF_ACCESS_TOKEN") or os.getenv("SF_CLIENT_ID"))
    dest_table.add_row("dryrun", ok, f"nothing — writes to {settings.output_dir}/")
    dest_table.add_row("hubspot", ok if hs else warn, "HUBSPOT_PRIVATE_APP_TOKEN")
    dest_table.add_row("salesforce", ok if sf else warn, "SF_ACCESS_TOKEN or SF_CLIENT_ID")
    console.print(dest_table)

    if not any_llm:
        console.print(
            "\n[dim]No LLM credentials found. `run` still works — it will use the "
            "keyword fallback and label the results heuristic:v1.[/dim]"
        )


@app.command()
def fields(
    dest: str = typer.Option("hubspot", help=f"One of: {', '.join(DESTINATIONS)}."),
    mapping: Path = typer.Option(Path("config/mapping.yaml"), help="Field mapping."),
) -> None:
    """Print the fields this destination writes, so you can create them first."""
    try:
        mapping_config = MappingConfig.load(mapping)
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    shape = "hubspot" if dest == "dryrun" else dest
    entries = mapping_config.for_destination(shape)
    if not entries:
        console.print(f"[yellow]No fields mapped for '{shape}'.[/yellow]")
        raise typer.Exit(code=1)

    table = Table(title=f"{shape} — {mapping_config.objects.get(shape, '?')}", header_style="bold")
    table.add_column("Enrichment field", style="cyan")
    table.add_column("API name")
    table.add_column("Type")
    for entry in entries:
        table.add_row(entry.source, entry.targets[shape], entry.value_type)
    console.print(table)
    console.print(
        f"\n[dim]Match key: {mapping_config.match_keys.get(shape)}. "
        "Create any missing custom fields before a live run — see docs/crm-setup.md.[/dim]"
    )


@app.command()
def scrape(
    domain: str = typer.Argument(..., help="Domain or URL, e.g. wistia.com."),
    scraper: str = typer.Option(None, help=f"Fetch backend: {', '.join(SCRAPERS)}."),
    out: Path = typer.Option(None, help="Write the markdown here instead of stdout."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Ignore the page cache."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch one homepage and show the markdown the analyzer will see."""
    _configure_logging(verbose)
    settings = _settings(scraper=scraper)

    async def go() -> None:
        normalized = normalize_domain(domain)
        async with build_client(settings.scrape) as client:
            robots = RobotsCache(client, settings.scrape.user_agent)
            page = await scrape_domain(
                client, robots, normalized, settings.scrape, use_cache=not no_cache
            )

        header = (
            f"# {page.title or normalized}\n\n"
            f"- url: {page.final_url}\n"
            f"- fetched: {page.fetched_at.isoformat()}"
            f"{' (from cache)' if page.from_cache else ''}\n"
            f"- backend: {settings.scrape.backend}\n"
            f"- vendors detected: {', '.join(page.tech_signals) or 'none'}\n"
            f"- links: {len(page.links)}\n\n---\n\n"
        )
        if out:
            out.write_text(header + page.markdown, encoding="utf-8")
            console.print(f"[green]Wrote[/green] {out} ({len(page.markdown):,} chars)")
        else:
            console.print(header + page.markdown)

    try:
        asyncio.run(go())
    except (FetchError, FetcherError, ValueError) as exc:
        console.print(f"[red]Scrape failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command()
def run(
    domain: list[str] = typer.Option(None, "--domain", "-d", help="Repeatable."),
    domains: Path = typer.Option(None, "--domains", help="CSV or txt file of domains."),
    dest: str = typer.Option("dryrun", help=f"One of: {', '.join(DESTINATIONS)}."),
    shape: str = typer.Option(
        "hubspot", help="Which destination's field names a dry run should emulate."
    ),
    scraper: str = typer.Option(None, help=f"Fetch backend: {', '.join(SCRAPERS)}."),
    provider: str = typer.Option(None, help=f"LLM provider: {', '.join(PROVIDER_NAMES)}."),
    model: str = typer.Option(None, help="Override the provider's default model."),
    icp: Path = typer.Option(Path("config/icp.yaml")),
    mapping: Path = typer.Option(Path("config/mapping.yaml")),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the keyword fallback."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Ignore page and analysis caches."),
    limit: int = typer.Option(0, help="Process at most N domains. 0 means all."),
    json_out: Path = typer.Option(None, "--json-out", help="Write full results as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Scrape, analyze, and write the results to a destination."""
    _configure_logging(verbose)
    settings = _settings(provider=provider, model=model, scraper=scraper)
    icp_profile, mapping_config = _load_configs(icp, mapping)

    targets = list(domain or [])
    if domains:
        targets.extend(read_domain_file(domains))
    if not targets:
        console.print("[red]Nothing to do:[/red] pass --domain or --domains.")
        raise typer.Exit(code=2)
    if limit > 0:
        targets = targets[:limit]

    active_provider = settings.analyze.provider
    use_llm = not no_llm and has_llm_credentials(active_provider)
    if not no_llm and not use_llm:
        console.print(
            f"[yellow]No {active_provider} credentials found — using the keyword "
            "fallback. Run `gtm-enrich check` to see what is missing.[/yellow]"
        )

    analyzer = (
        f"{active_provider}:{settings.analyze.model or default_model_for(active_provider)}"
        if use_llm
        else "heuristic:v1"
    )
    write_shape = shape if dest == "dryrun" else dest
    console.print(
        f"[bold]{len(targets)}[/bold] domains → scraper [bold]{settings.scrape.backend}[/bold] "
        f"→ analyzer [bold]{analyzer}[/bold] "
        f"→ destination [bold]{dest}[/bold] ({write_shape} field names)"
    )

    with console.status("Scraping and analyzing…"):
        results = asyncio.run(
            enrich_domains(
                targets,
                settings,
                icp_profile,
                use_llm=use_llm,
                use_cache=not no_cache,
                progress=lambda d, s: None,
            )
        )

    console.print(_results_table(results))

    try:
        destination = build_destination(
            dest,
            object_type=mapping_config.objects.get(write_shape, "companies"),
            field_names=mapping_field_names(mapping_config, write_shape),
            output_dir=settings.output_dir,
            shape=write_shape,
        )
    except DestinationError as exc:
        console.print(f"[red]Destination error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    with destination:
        writes = write_results(results, mapping_config, destination, write_shape)
        if isinstance(destination, DryRunDestination):
            paths = destination.flush()
            console.print(
                f"\n[green]Dry run:[/green] no API calls made. "
                f"Payloads → {paths['json']}\n              Flat CSV → {paths['csv']}"
            )

    _print_write_summary(writes, results, settings)

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(
                {
                    "results": [r.model_dump() for r in results],
                    "writes": [w.model_dump() for w in writes],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        console.print(f"[green]Wrote[/green] {json_out}")

    if any(w.action == "failed" for w in writes):
        raise typer.Exit(code=1)


def _print_write_summary(writes, results, settings) -> None:
    counts: dict[str, int] = {}
    for w in writes:
        counts[w.action] = counts.get(w.action, 0) + 1
    console.print("  ".join(f"{k}: [bold]{v}[/bold]" for k, v in sorted(counts.items())))

    for w in writes:
        if w.action == "failed":
            console.print(f"  [red]{w.domain}[/red]: {w.error}")

    def totals(rows) -> tuple[int, float, bool]:
        tokens = sum(
            (r.provenance.input_tokens or 0) + (r.provenance.output_tokens or 0)
            for r in rows
            if r.provenance
        )
        cost = sum(r.provenance.cost_usd or 0.0 for r in rows if r.provenance)
        priced = any(r.provenance and r.provenance.cost_usd for r in rows)
        return tokens, cost, priced

    analyzed = [r for r in results if r.provenance and not r.from_cache]
    replayed = [r for r in results if r.provenance and r.from_cache]

    tokens, cost, priced = totals(analyzed)
    if tokens and priced:
        console.print(
            f"[dim]{tokens:,} tokens this run, estimated ${cost:.4f} at list price.[/dim]"
        )
    elif tokens:
        console.print(
            f"[dim]{tokens:,} tokens this run. No published price on file for this model, "
            "so no cost estimate.[/dim]"
        )

    if replayed:
        # Spend that already happened. Printing it as though it were incurred
        # again is how a caching tool ends up looking like it isn't caching.
        rtokens, rcost, rpriced = totals(replayed)
        detail = f" (originally {rtokens:,} tokens, ${rcost:.4f})" if rtokens and rpriced else ""
        console.print(
            f"[dim]{len(replayed)} of {len(results)} served from the analysis cache — "
            f"no API call, no new spend{detail}.[/dim]"
        )


if __name__ == "__main__":
    app()
