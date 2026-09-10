# gtm-enrich

[![CI](https://github.com/robertboconnor/gtm-enrich/actions/workflows/ci.yml/badge.svg)](https://github.com/robertboconnor/gtm-enrich/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Scrape a company's homepage, ask a fixed set of GTM questions about what's there,
and write the answers back into **Salesforce** or **HubSpot** as structured fields.

Bring your own scraper (**direct**, **Firecrawl**, **crawl4ai**, or **Apify**) and
your own model (**Claude** or **GPT**). Point it at your own CRM.

> Built by a RevOps operator. The design bias throughout is **dry-run first**:
> build the exact payload, show it, write nothing until told to.

```
domains ──▶ scrape ──▶ markdown ──▶ analyze ──▶ map ──▶ write
              │           │            │          │        │
         robots.txt  deterministic  Claude /   config/   HubSpot
         + caching     signals        GPT      mapping   Salesforce
                      (no LLM)     structured   .yaml    dry run
```

## What's in the box

| Piece | What it does |
| --- | --- |
| `scrape/fetchers/` | Four fetch backends behind one interface — `direct` (plain HTTP, no key), `firecrawl`, `crawl4ai` (self-hosted), `apify`. Three of them render JavaScript. |
| `analyze/providers/` | Two LLM providers behind one interface — **Anthropic** and **OpenAI** — both using schema-enforced structured output, plus a keyword fallback that needs no key at all. |
| `scrape/markdown.py` | HTML → markdown, and the deterministic signals: vendor fingerprints and structural facts, parsed rather than inferred. |
| `mapping.py` + `config/mapping.yaml` | Enrichment fields → CRM API names, per destination. No field name is hardcoded anywhere. |
| `config/icp.yaml` | What "good fit" means. Injected into the prompt, so editing it changes every score. |
| `destinations/` | `dryrun`, `hubspot`, `salesforce` — all sharing one find → diff → write upsert. |
| `cli.py` | `check`, `fields`, `scrape`, `run`. |

## Requirements

- **Python 3.10+**
- Optionally an **Anthropic** or **OpenAI** key — without one, runs use the keyword fallback
- Optionally a **Firecrawl** / **Apify** key or a local **crawl4ai** container — without one, scraping uses plain HTTP
- Optionally a **HubSpot private app token** or **Salesforce** credentials — without either, writes go to disk

Every backend talks raw HTTP over `httpx`. There is no Firecrawl, crawl4ai, or
Apify SDK to install.

## Quick start

No credentials required. This works from a clean clone:

```bash
pip install -e ".[dev]"
gtm-enrich run --domains examples/domains.csv --dest dryrun
```

That scrapes five real homepages, analyzes them, and writes the exact payloads it
*would* send to HubSpot into `out/`.

```bash
cp .env.example .env      # then fill in whichever keys you have
gtm-enrich check          # what's wired up, what isn't, what each thing needs
```

`check` is the map of the whole system:

```
                            LLM providers
┏━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ Provider             ┃ Status  ┃ Default model ┃ Needs             ┃
┡━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ anthropic (selected) │ ok      │ claude-opus-5 │ ANTHROPIC_API_KEY │
│ openai               │ not set │ gpt-5-mini    │ OPENAI_API_KEY    │
└──────────────────────┴─────────┴───────────────┴───────────────────┘
                                Scraper backends
┏━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Backend           ┃ Status       ┃ Renders JS ┃ Needs                     ┃
┡━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ direct (selected) │ ok           │ no         │ nothing — built in        │
│ firecrawl         │ ok           │ yes        │ FIRECRAWL_API_KEY         │
│ crawl4ai          │ needs server │ yes        │ a running crawl4ai server │
│ apify             │ not set      │ yes        │ APIFY_API_TOKEN           │
└───────────────────┴──────────────┴────────────┴───────────────────────────┘
```

### Which backend?

Measured on the five domains in `examples/domains.csv`, same day, same code —
markdown extracted and vendors detected:

| Domain | `direct` | `firecrawl` | | Vendors found |
| --- | ---: | ---: | --- | --- |
| stripe.com | 14,167 | 35,875 | 2.5× | Firecrawl also caught Google Tag Manager |
| linear.app | 10,424 | 18,584 | 1.8× | same |
| gong.io | 13,419 | 35,411 | 2.6× | same |
| wistia.com | 4,061 | 19,039 | 4.7× | **Firecrawl also caught Wistia** |
| notion.so | 1,366 | 14,498 | 10.6× | same |

`direct` is free and fine for server-rendered pages. On JavaScript-heavy sites it
sees a fraction of the page — notion.so came back at 1,366 characters, barely
above the threshold where this tool gives up and tells you to switch backends.

The wistia.com row is the one worth dwelling on: a plain HTTP fetch could not see
that Wistia's own homepage embeds a Wistia player, because the embed is injected
at runtime. Lazy-loaded analytics and video tags are invisible to `direct`, and
that is a silent wrong answer rather than an error.

Then mix and match:

```bash
gtm-enrich scrape gong.io --scraper firecrawl
gtm-enrich run --domains accounts.csv --provider openai --model gpt-5-nano
gtm-enrich run --domains accounts.csv --scraper apify --dest hubspot
gtm-enrich fields --dest salesforce      # the fields to create before a live run
```

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
    "gtm_enrichment_source": "anthropic:claude-opus-5",
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

## The two files an ops person edits

Neither requires touching Python.

**`config/icp.yaml`** — what "good fit" means, injected verbatim into the prompt:

```yaml
name: "B2B video platform ICP"
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

See [docs/crm-setup.md](docs/crm-setup.md) for creating the fields and getting
credentials in each system.

## Design decisions

The parts worth talking through.

**Structured outputs, not prompt-and-hope.** Both providers are handed the same
Pydantic model as a JSON schema — `output_format` on Anthropic, `text_format` on
OpenAI — so neither can return something that fails to parse. That is what makes
it safe to write into a CRM with no human in the loop: the failure mode is a bad
*value*, never a malformed record. The common alternative — "return ONLY a JSON
object, no markdown fences" — works until the day it doesn't, and then it fails
silently as a field that never got set.

**Two vendor interfaces, normalized to one.** `LLMResult` hides more than it
looks like. OpenAI reports cached tokens *inside* `input_tokens`; Anthropic
reports them alongside. Anthropic has five effort levels, OpenAI four. Anthropic
signals refusal via `stop_reason`, OpenAI via an `incomplete` status. All of that
is absorbed at the provider boundary, so cost accounting means the same thing on
both and `--effort xhigh` doesn't 400 just because you switched vendor.

**Four scrapers, one contract.** A fetcher answers one question: what is on this
page? It does not decide *whether* to fetch (robots), *which* URL to try (apex vs
www), or what to do with the result. Those policies live once, in
`scrape/fetch.py`, so they are identical no matter which backend you pick. Some
backends return markdown, some only HTML; `build_page` handles either, and a
markdown-only backend degrades in a defined way — links still parse, vendor
detection is empty, because the tags it reads no longer exist.

**Raw HTML, not cleaned HTML.** Firecrawl offers both, and the difference is not
cosmetic: the cleaned `html` format strips every `<script>` tag, which is exactly
where `js.hs-scripts.com`, `googletagmanager.com`, and every other vendor
fingerprint lives. Measured on one real homepage, cleaned HTML contained 0 script
tags and raw HTML contained 44. Asking for the wrong one returns zero detected
vendors forever, with no error anywhere — the kind of bug that only shows up
against a live API, which is why this one was found by running it and not by a
test.

**Deterministic signals are kept separate from judgement.** A `/pricing` link
either exists or it doesn't; `js.hs-scripts.com` is either on the page or it
isn't. Those are extracted with a parser and passed to the model as facts it
isn't allowed to contradict. Only genuinely interpretive questions — segment, ICP
fit, category — are left to the model. Vendor fingerprints match anchored asset
URLs, never bare words, so a page that merely *mentions* a vendor on a comparison
page doesn't count as using it.

**Provenance on every record.** Four fields ride along with every write: source
URL, timestamp, analyzer (`anthropic:claude-opus-5`, `openai:gpt-5-nano`, or
`heuristic:v1`), and a hash of the page content that produced it. The first time
someone disputes a score, you can answer where it came from — and the hash tells
you whether the page has changed since.

**Idempotent by construction.** `Destination.upsert` is find → diff → write, and
the diff normalizes types before comparing, because CRMs round-trip `78` as
`"78"` and `true` as `"true"`. Re-running the whole batch writes nothing if
nothing changed. Implemented once in the base class, so every destination gets
it — including ones added later.

**Dry run is a real destination, not a demo mode.** It builds the identical
payload through the identical mapping and coercion, then writes it to disk
instead of sending it. `--dest dryrun --shape salesforce` shows you the exact
Salesforce payload without an org. It's the thing you run *before* a live load.

**Caching at both expensive steps.** Pages are cached for a week; analyses are
cached against a key of `content hash + provider:model + ICP fingerprint`. Edit
`icp.yaml` and every cached score correctly invalidates — same page, different
question. Switch provider and you get a fresh answer rather than a stale one
attributed to the wrong model.

**Prompt caching is designed for, not bolted on.** The ICP block is
byte-identical across every domain in a run and goes first — carrying an explicit
cache breakpoint on Anthropic, sitting in the auto-cached prefix on OpenAI.
Everything per-account goes after it, and cache reads are folded into the
per-run cost estimate.

**Costs are reported, never invented.** Token counts always print. A dollar
figure only appears for models with a published price on file; an unlisted model
reports tokens and says there's no estimate, rather than guessing.

**Politeness, since this hits sites owned by real people.** A declared
User-Agent, `robots.txt` checked before the first request *on every backend*,
bounded concurrency, `Retry-After` honoured, and a cache so a re-run costs nobody
any bandwidth. The Apify token goes in a header rather than the `?token=` query
parameter their examples use, so it stays out of URLs and logs.

**One bad account never sinks a batch.** Invalid domains, fetch failures,
JavaScript-only pages, model refusals, and CRM field errors all become recorded
per-row failures. Model failure degrades to the heuristic analyzer rather than
dropping the record. A misconfigured *backend*, by contrast, fails the whole run
immediately — that's a config error, not a data error, and it should be loud.

## Safety model

- **Dry run is the default.** `--dest dryrun` writes to `out/` and makes no API
  calls. You have to name a real destination to touch a CRM.
- **Writes are diffed first.** Nothing is sent unless a value would actually change.
- **Credentials live in `.env`**, which is gitignored, or in your shell. Real
  environment variables always beat the file, so CI is never overridden by a
  stale local `.env`.
- **Scraped pages and generated payloads** land in `.cache/` and `out/`, both
  gitignored, so nothing you scrape ends up in git.
- **`GTM_RESPECT_ROBOTS=1` by default.** Turn it off only for sites you own.

## Tests

```bash
pytest -q                  # 132 tests, no network, no credentials
ruff check src tests
```

Every test runs against mocked HTTP transports and stubbed SDK clients, so CI is
hermetic. The suite covers the things most likely to break quietly: robots.txt
refusals, the www fallback, JS-only shells, all four backends' response shapes
(including crawl4ai's three response envelopes, Firecrawl's list-valued metadata,
and a regression test pinning the raw-vs-cleaned HTML choice above), per-destination
type coercion, no-op suppression, `Retry-After`
handling, SOQL injection guards, both providers' refusal paths, token
normalization across vendors, and cache invalidation.

## What this isn't

- **Only `direct` and `firecrawl` are live-tested.** Both have been run against
  real sites. `crawl4ai` and `apify` are built to their documented API shapes and
  covered by tests against mocked transports, but no successful call has been
  made to either. Same for the OpenAI provider — the request shape is verified,
  a real completion is not.
- **The homepage only.** No crawling to `/about`, `/pricing`, or `/customers`,
  which is where a lot of the real signal lives.
- **The keyword fallback is genuinely crude.** It's substring matching against a
  keyword list, it's capped at 0.4 confidence, and it labels itself
  `heuristic:v1` in provenance so nobody mistakes it for analysis. It exists so
  the pipeline runs with no API key at all.
- **The Salesforce matcher uses `Website LIKE`,** which is fine for a POC and
  wrong for a large org. [docs/crm-setup.md](docs/crm-setup.md#3-matching-and-why-you-should-change-it)
  explains the external-ID field you'd use instead.
- **No scheduler.** It's a CLI. Cron it, or wrap it in whatever you already run.

## Layout

```
src/gtm_enrich/
├── models.py            # typed contract for every stage
├── config.py            # .env + env for secrets, YAML for the ops-editable parts
├── pipeline.py          # orchestration + caching
├── mapping.py           # enrichment fields → CRM API names
├── cli.py               # check / fields / scrape / run
├── scrape/
│   ├── fetch.py         # robots, apex/www fallback, cache, page assembly
│   ├── markdown.py      # HTML → markdown + deterministic signals
│   └── fetchers/        # direct · firecrawl · crawl4ai · apify
├── analyze/
│   ├── prompt.py        # the questions, and the cached ICP prefix
│   ├── heuristics.py    # no-key keyword fallback
│   └── providers/       # anthropic · openai
└── destinations/        # base upsert cycle · dryrun · hubspot · salesforce
```

MIT — see [LICENSE](LICENSE).
