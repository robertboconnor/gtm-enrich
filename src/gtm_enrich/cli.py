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

from .config import (
    ConfigError,
    IcpProfile,
    MappingConfig,
    Settings,
    has_anthropic_credentials,
)
from .destinations import DESTINATIONS, DestinationError, build_destination
from .destinations.dryrun import DryRunDestination
from .mapping import mapping_field_names
from .models import EnrichmentResult
from .pipeline import enrich_domains, write_results
from .scrape.fetch import FetchError, RobotsCache, build_client, normalize_domain, scrape_domain

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Scrape a homepage, answer GTM questions about it, write the answers to a CRM.",
)
console = Console()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


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
    settings = Settings.from_env()
    icp_profile, mapping_config = _load_configs(icp, mapping)

    table = Table(title="gtm-enrich check", header_style="bold")
    table.add_column("Item")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    ok, warn = "[green]ok[/green]", "[yellow]not set[/yellow]"
    table.add_row("ICP config", ok, f"{icp} — {icp_profile.name}")
    table.add_row("Field mapping", ok, f"{mapping} — {len(mapping_config.fields)} fields")

    llm = has_anthropic_credentials()
    table.add_row(
        "Anthropic credentials",
        ok if llm else warn,
        f"model {settings.analyze.model}" if llm else "runs will fall back to heuristics",
    )
    hs = bool(os.getenv("HUBSPOT_PRIVATE_APP_TOKEN"))
    table.add_row("HubSpot", ok if hs else warn, "HUBSPOT_PRIVATE_APP_TOKEN")
    sf = bool(os.getenv("SF_ACCESS_TOKEN") or os.getenv("SF_CLIENT_ID"))
    table.add_row("Salesforce", ok if sf else warn, "SF_ACCESS_TOKEN or SF_CLIENT_ID")
    table.add_row("Dry run", ok, f"always available — writes to {settings.output_dir}/")

    console.print(table)
    if not llm:
        console.print(
            "\n[dim]No Anthropic credentials found. `run` still works — it will use the "
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
    out: Path = typer.Option(None, help="Write the markdown here instead of stdout."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Ignore the page cache."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch one homepage and show the markdown the analyzer will see."""
    _configure_logging(verbose)
    settings = Settings.from_env()

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
    except (FetchError, ValueError) as exc:
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
    settings = Settings.from_env()
    icp_profile, mapping_config = _load_configs(icp, mapping)

    targets = list(domain or [])
    if domains:
        targets.extend(read_domain_file(domains))
    if not targets:
        console.print("[red]Nothing to do:[/red] pass --domain or --domains.")
        raise typer.Exit(code=2)
    if limit > 0:
        targets = targets[:limit]

    use_llm = not no_llm and has_anthropic_credentials()
    if not no_llm and not use_llm:
        console.print(
            "[yellow]No Anthropic credentials found — using the keyword fallback.[/yellow]"
        )

    write_shape = shape if dest == "dryrun" else dest
    console.print(
        f"[bold]{len(targets)}[/bold] domains → analyzer "
        f"[bold]{'llm:' + settings.analyze.model if use_llm else 'heuristic:v1'}[/bold] "
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

    cost = sum(
        r.provenance.cost_usd or 0.0
        for r in results
        if r.provenance and r.provenance.cost_usd
    )
    tokens = sum(
        (r.provenance.input_tokens or 0) + (r.provenance.output_tokens or 0)
        for r in results
        if r.provenance
    )
    if tokens:
        console.print(
            f"[dim]{tokens:,} tokens, estimated ${cost:.4f} at "
            f"{settings.analyze.model} list price.[/dim]"
        )


if __name__ == "__main__":
    app()
