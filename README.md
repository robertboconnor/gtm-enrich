# gtm-enrich

[![CI](https://github.com/robertboconnor/gtm-enrich/actions/workflows/ci.yml/badge.svg)](https://github.com/robertboconnor/gtm-enrich/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Scrape a company's homepage, ask a fixed set of GTM questions about what's there,
and write the answers back into Salesforce or HubSpot as structured fields.

It's the shape of enrichment problem that usually gets solved with a vendor
subscription and a Zapier chain: you have a list of domains, you want more than a
firmographic row about each one, and you want it in the CRM where reps actually
work. This does it as a small, testable pipeline you own.

Built as a portfolio project — it runs, it's tested, and the design decisions are
the interesting part.

```
domains ──▶ scrape ──▶ markdown ──▶ analyze ──▶ map ──▶ write
             │           │            │          │        │
        robots.txt   deterministic  Claude   config/   HubSpot
        + caching      signals    structured mapping   Salesforce
                    (no LLM)       output     .yaml    dry run
```

---

## Quick start

No credentials required. This works from a clean clone:

```bash
pip install -e ".[dev]"
gtm-enrich run --domains examples/domains.csv --dest dryrun
```

That scrapes five real homepages, analyzes them, and writes the exact payloads it
*would* send to HubSpot into `out/`. Add an `ANTHROPIC_API_KEY` and the analysis
step upgrades from keyword matching to actual reading.

```bash
gtm-enrich check                      # what's configured, what's missing
gtm-enrich scrape gong.io             # see the markdown the analyzer sees
gtm-enrich fields --dest salesforce   # the fields you need to create first
gtm-enrich run --domain acme.com --dest hubspot
```

---

## What it produces

Each domain becomes a CRM-ready payload, with real API field names. This is the
shape, with LLM analysis enabled:

```json
{
  "domain": "gong.io",
  "object_type": "companies",
  "match_key": "domain",
  "properties": {
    "name": "Gong",
    "gtm_category": "revenue intelligence",
    "gtm_sells_to": "B2B",
    "gtm_segment": "Enterprise",
    "gtm_icp_fit_score": 78,
    "gtm_icp_fit_rationale": "Sells to revenue teams and runs an obvious content program...",
    "gtm_buying_signals": "Careers page is live (hiring); Analyst recognition claimed",
    "gtm_tech_signals": "Google Tag Manager; Next.js; OneTrust",
    "gtm_has_pricing_page": "true",
    "gtm_has_demo_cta": "true",
    "gtm_enriched_from_url": "https://www.gong.io/",
    "gtm_enriched_at": "2026-09-10T17:33:47Z",
    "gtm_enrichment_source": "llm:claude-opus-5",
    "gtm_source_content_hash": "a3f1c8b2e5d40917"
  }
}
```

The dry-run destination writes exactly this to `out/payloads-*.json`, alongside a
flat CSV for anyone who'd rather look at it in a spreadsheet.

### The questions it answers

Company name · one-line description · product category · B2B/B2C · size segment ·
business model · industries served · **ICP fit score 0–100** · rationale ·
buying signals · disqualifiers · primary CTA · confidence · supporting quotes.

Plus signals read straight off the HTML, with no model involved: detected vendors
(HubSpot, Marketo, Segment, Stripe, Webflow, …) and structural facts (has a
pricing page, a demo CTA, a careers page, case studies, a security page).

---

## The two files an ops person edits

Neither requires touching Python.

**`config/icp.yaml`** — what "good fit" means. It's injected verbatim into the
analysis prompt, so changing it changes every score:

```yaml
name: "B2B video platform ICP"
description: >
  We sell a video hosting and video marketing platform to B2B software and
  services companies...
good_fit:
  - "publishes a blog, webinars, customer stories, or a resource library"
  - "uses a marketing automation platform such as HubSpot or Marketo"
poor_fit:
  - "video hosting or video platform competitor"
```

**`config/mapping.yaml`** — enrichment field → CRM API name, per destination:

```yaml
fields:
  - source: analysis.icp_fit_score
    type: number
    hubspot: gtm_icp_fit_score
    salesforce: GTM_ICP_Fit_Score__c
```

Add a column, that destination starts writing the field. Delete it, it stops.
Nothing in the code knows what a field is called in your org.

See [docs/crm-setup.md](docs/crm-setup.md) for creating the fields and getting
credentials in each system.

---

## Design decisions

The parts I'd actually want to talk through in an interview.

**Structured outputs, not prompt-and-hope.** The analysis schema is a Pydantic
model handed to the API as a JSON schema, so the model physically cannot return
something that fails to parse. That's what makes it safe to write into a CRM with
no human in the loop — the failure mode is a bad *value*, never a malformed
record. Every field's docstring is prompt surface area and is written as such.

**Deterministic signals are kept separate from judgement.** A `/pricing` link
either exists or it doesn't; `js.hs-scripts.com` is either on the page or it
isn't. Those are extracted with a parser and passed to the model as facts it
isn't allowed to contradict. Only genuinely interpretive questions — segment, ICP
fit, category — are left to the model. It shrinks the surface area for
hallucination and keeps the pipeline useful with the model turned off entirely.

**Provenance on every record.** Four fields ride along with every write: source
URL, timestamp, analyzer version (`llm:claude-opus-5` or `heuristic:v1`), and a
hash of the page content that produced it. The first time someone disputes a
score, you can answer where it came from — and the hash tells you whether the
page has changed since.

**Idempotent by construction.** `Destination.upsert` is find → diff → write, and
the diff normalizes types before comparing, because CRMs round-trip `78` as
`"78"` and `true` as `"true"`. Re-running the whole batch writes nothing if
nothing changed. That's implemented once in the base class, so every destination
gets it — including ones added later.

**Dry run is a real destination, not a demo mode.** It builds the identical
payload through the identical mapping and coercion, then writes it to disk
instead of sending it. `--dest dryrun --shape salesforce` shows you the exact
Salesforce payload without an org. It's the thing you run *before* a live load.

**Caching at both expensive steps.** Pages are cached for a week; analyses are
cached against a key of `content hash + analyzer + ICP fingerprint`. Edit
`icp.yaml` and every cached score correctly invalidates — same page, different
question. Re-running a 500-account list after a config change costs one API call
per account whose page actually changed.

**Prompt caching is designed for, not bolted on.** The ICP block is byte-identical
across every domain in a run and carries the cache breakpoint; everything
per-account goes after it. A 50-account batch pays for the ICP definition once,
and `cache_read_input_tokens` is rolled into the per-run cost estimate.

**Politeness, since this hits sites owned by real people.** A declared
User-Agent, `robots.txt` checked before the first request, bounded concurrency,
`Retry-After` honoured on rate limits, and a cache so a re-run costs nobody any
bandwidth.

**One bad account never sinks a batch.** Invalid domains, fetch failures,
JavaScript-only pages, model refusals, and CRM field errors all become recorded
per-row failures. Model failure specifically degrades to the heuristic analyzer
rather than dropping the record.

---

## Tests

```bash
pytest -q     # 84 tests, no network, no credentials
ruff check src tests
```

Everything runs against mocked HTTP transports and a stubbed Anthropic client, so
CI is hermetic. The suite covers the things most likely to break quietly:
robots.txt refusals, the www fallback, JS-only shells, per-destination type
coercion, no-op suppression, `Retry-After` handling, SOQL injection guards, the
refusal path, and cache invalidation.

---

## What this isn't

Worth being straight about the limits:

- **No JavaScript rendering.** Pages that ship an empty `<div id="root">` are
  detected and skipped rather than silently analyzed as blank. Adding Playwright
  is the obvious next step and would slot in behind the same `ScrapedPage`.
- **The homepage only.** No crawling to `/about`, `/pricing`, or `/customers`,
  which is where a lot of the real signal lives.
- **The heuristic fallback is genuinely crude.** It's keyword matching, it's
  capped at 0.4 confidence, and it labels itself `heuristic:v1` in provenance so
  nobody mistakes it for analysis. It exists so the pipeline runs with no API key.
- **The Salesforce matcher uses `Website LIKE`,** which is fine for a POC and
  wrong for a large org. [docs/crm-setup.md](docs/crm-setup.md#3-matching-and-why-you-should-change-it)
  explains the external-ID field you'd use instead.
- **No scheduler.** It's a CLI. Cron it, or wrap it in whatever you already run.

## Layout

```
src/gtm_enrich/
├── models.py          # typed contract for every stage
├── config.py          # env for secrets, YAML for the ops-editable parts
├── pipeline.py        # orchestration + caching
├── mapping.py         # enrichment fields → CRM API names
├── cli.py             # check / fields / scrape / run
├── scrape/            # fetch (robots, retries, cache) + HTML → markdown
├── analyze/           # prompt, Claude structured output, keyword fallback
└── destinations/      # base upsert cycle, dry run, HubSpot, Salesforce
```

MIT licensed.
