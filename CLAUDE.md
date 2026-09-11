# CLAUDE.md — gtm-enrich

Operating notes for Claude Code (or any other coding agent) working in this repo.

**What this is.** A homepage enrichment pipeline for a GTM stack. It pulls a list
of accounts out of HubSpot or Salesforce (or a CSV), scrapes each company's
homepage, asks a fixed set of GTM questions about it with Claude or GPT, and
writes the answers back as structured CRM fields. Run by hand, on a schedule, or
in real time off a webhook.

**What it is for.** This is a public reference implementation, meant to be read,
run, and **adapted to whatever stack the user actually has**. It is not a product
to install and leave alone. Assume the person you are working for wants to point
it at their own CRM, not admire it.

**If they just cloned this and want to see it work, run "Prove it works" below.**
It needs no credentials, no API keys, and no CRM. Do it before anything else.

## Prove it works (no credentials, about two minutes)

Run everything from the repo root — `config/` paths resolve relative to the
working directory.

**Python 3.10+ is required, and the system `python3` on macOS is 3.9.** Pick an
interpreter first rather than assuming:

```bash
for v in python3.13 python3.12 python3.11 python3.10 python3; do
  command -v $v >/dev/null 2>&1 && $v -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null && PY=$v && break
done; echo "using ${PY:-NONE FOUND}"
```

If that prints `NONE FOUND`, stop and tell the user they need Python 3.10 or
newer — `brew install python@3.13` on a Mac, or python.org. Do not try to work
around it.

```bash
$PY -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
.venv/bin/gtm-enrich run --domains examples/domains.csv --dest dryrun
```

**Upgrading pip is not optional.** pip older than 21.3 cannot install this
project and fails with `File "setup.py" or "setup.cfg" not found` — which sounds
like a missing file and is really a pip too old for a `pyproject.toml`-only
build. Stock macOS ships pip 21.2.

That command scrapes five real homepages, analyzes them, and writes the exact
payloads it *would* send to HubSpot into `out/`. Then show the user:

- the results table printed to the terminal — one row per account, with an ICP
  fit score
- `out/payloads-hubspot-*.json` — the real CRM payload, real API field names
- `examples/output/` — committed output from a run *with* a model, for comparison

**With no API key the run still works and is visibly worse.** It falls back to
keyword matching, pins confidence at `0.40`, labels itself `heuristic:v1` in the
provenance, and gets things wrong — gong.io comes back as `crm` rather than
revenue intelligence. That is expected and honest, not a bug. Say so. The
committed output in `examples/output/` shows the same five accounts analyzed by
`claude-opus-5`, including the competitor that scored 2 out of 100.

## Verify it, don't assert it

```bash
.venv/bin/pytest -q          # 216 tests, no network, no credentials
.venv/bin/ruff check src tests
.venv/bin/gtm-enrich check   # what's wired up, what isn't, what each thing needs
```

`check` is the map of the system — run it before diagnosing any "it doesn't
work" report. It names the missing environment variable rather than making the
user guess.

## Adapting it to someone's stack

This is the main event. In order:

1. **`config/icp.yaml`** — what "good fit" means for their business. Injected
   verbatim into the prompt, so editing it changes every score. Editing it also
   correctly invalidates cached analyses.
2. **`config/mapping.yaml`** — enrichment field → CRM API name, per destination.
   No field name is hardcoded in Python. Add a column and that destination starts
   writing the field; delete it and it stops.
3. **`gtm-enrich fields --dest hubspot`** — prints the properties that have to
   exist in the target system *before* a live run. Create them first;
   HubSpot rejects a whole query for one missing property without saying which.
4. **`config/filters/*.yaml`** — which accounts to pull. One definition compiles
   into a HubSpot search body or a SOQL `WHERE` clause.
5. **`gtm-enrich preview --source hubspot`** — shows the compiled query and what
   it matches, enriching nothing. Needs the CRM token; without one it tells you
   which variable is missing and stops.

Then dry run, then live. Never skip to live.

## Rules

- **Never run `--dest hubspot` or `--dest salesforce` unless the user explicitly
  asked for a live write in that message.** Those write to a production CRM.
  `--dest dryrun` is the default for a reason; keep it that way.
- **A dry run is not free.** It skips the CRM write, not the scrape and not the
  model call. It costs whatever the scraper and model cost. Do not tell a user a
  dry run is free — tell them it costs money but cannot damage their CRM.
- **Never turn off `GTM_RESPECT_ROBOTS`** except for sites the user owns, and say
  what you are doing when you do.
- **Never commit `.env`, `out/`, or `.cache/`.** All three are gitignored and
  contain credentials or scraped third-party content.
- **Report what actually happened.** If a run failed on three of five accounts,
  say that and name them. Per-row failures are recorded on purpose.

## Gotchas already paid for — don't rediscover these

- **Firecrawl: ask for `rawHtml`, not `html`.** The cleaned `html` strips every
  `<script>` tag, which is exactly where vendor fingerprints live. Measured on one
  homepage: cleaned had 0 script tags, raw had 44. Wrong answer, no error. There
  is a regression test pinning this.
- **HubSpot ANDs within a filter group and ORs between groups**, with no way to
  express `A AND (B OR C)` directly. `or_unknown` expands into a cross product,
  so each one doubles the group count. The expansion is capped deliberately.
- **Page existence is not a status code.** Three of seven well-known B2B sites
  return `200` for a URL that cannot exist. `scrape/probe.py` calibrates against
  a control request per site. Note that `probe` is a standalone command, *not* a
  step inside `run` — the `gtm_has_*` fields written to the CRM are read off the
  homepage's own links.
- **Effort is `GTM_EFFORT` in the environment, not a `--effort` flag.**
- **`gtm-enrich serve` needs the server extra** — `pip install -e ".[server]"`.
  `.[dev]` already includes it.
- **Webhooks must listen for the website property *changing*,** not just the
  record being created. CRM records are usually created empty and the website
  lands a second later.

## Where things are

- `src/gtm_enrich/pipeline.py` — orchestration and both caches
- `src/gtm_enrich/models.py` — the typed contract for every stage; the analysis
  schema here is also the prompt surface handed to the model
- `src/gtm_enrich/sources/` — csv · hubspot · salesforce (where the list comes from)
- `src/gtm_enrich/scrape/` — `fetch.py` (robots, apex/www, cache), `markdown.py`
  (HTML → markdown + deterministic signals), `probe.py`, `fetchers/` (direct ·
  firecrawl · crawl4ai · apify)
- `src/gtm_enrich/analyze/` — `prompt.py`, `heuristics.py` (no-key fallback),
  `providers/` (anthropic · openai)
- `src/gtm_enrich/destinations/` — one find → diff → write upsert, shared by
  dryrun · hubspot · salesforce
- `src/gtm_enrich/server/` — webhook service, durable SQLite queue, worker
- `docs/deployment.md` — manual, scheduled, and real-time triggers
- `docs/crm-setup.md` — creating the fields and getting credentials

Known-unproven, and the README says so plainly: `crawl4ai` and `apify` have never
made a successful call, and no live webhook has ever been pointed at the service.
Do not claim otherwise.

---

`AGENTS.md` and `CLAUDE.md` are identical apart from their first two lines. Edit
both, or neither.
