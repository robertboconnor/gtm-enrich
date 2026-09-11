# gtm-enrich

[![CI](https://github.com/robertboconnor/gtm-enrich/actions/workflows/ci.yml/badge.svg)](https://github.com/robertboconnor/gtm-enrich/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Pull a list of accounts **out of** Salesforce or HubSpot, scrape each company's
homepage, ask a fixed set of GTM questions about what's there, and write the
answers back **into** the CRM as structured fields.

Run it by hand, on a schedule, or in real time as records change. Bring your own
scraper (**direct**, **Firecrawl**, **crawl4ai**, or **Apify**) and your own model
(**Claude** or **GPT**). Point it at your own stack.

**It is built to be run through a coding agent.** Clone it, open it in Claude
Code or Codex — or hand the Markdown to whatever else you use — and say *"read
AGENTS.md and show me this working."* The agent installs it, enriches five real
companies with no credentials at all, and walks you through the output. Then
tell it about your CRM and it adapts this to you: the architecture is already
decided, so what's left is your ICP, your field names, and your keys.

[**AGENTS.md**](AGENTS.md) and [**CLAUDE.md**](CLAUDE.md) are that briefing —
how to prove it works, how to point it at a new stack, the rules that keep it
away from your production CRM, and the mistakes already paid for so nobody pays
for them twice.

> Built by a RevOps operator. The design bias throughout is **dry-run first**:
> build the exact payload, show it, write nothing until told to.

```
   WHERE THE LIST COMES FROM          THE PIPELINE              WHERE IT GOES
  ┌──────────────────────┐
  │ CSV / --domain       │──┐
  │ HubSpot + filter     │──┼──▶ scrape ─▶ analyze ─▶ map ─▶ HubSpot
  │ Salesforce + filter  │──┘      │          │        │     Salesforce
  └──────────────────────┘         │          │        │     dry run
                                robots.txt  Claude/  config/
     triggered by: you,          + caching    GPT    mapping
     a schedule, or a webhook   determinstic schema-   .yaml
                                  signals   enforced
```

## What's in the box

| Piece | What it does |
| --- | --- |
| `sources/` | Where the account list comes from — a CSV, or a filtered query against HubSpot or Salesforce. |
| `filters.py` + `config/filters/` | One filter definition, compiled into a HubSpot search body *or* a SOQL `WHERE` clause. |
| `server/` | The webhook service: verify, deduplicate, enqueue, return. Plus a durable job queue and worker. |
| `state.py` | Run watermarks and event dedupe, so scheduled runs stay incremental and webhook retries don't double-bill. |
| `scrape/fetchers/` | Four fetch backends behind one interface — `direct` (plain HTTP, no key), `firecrawl`, `crawl4ai` (self-hosted), `apify`. Three of them render JavaScript. |
| `analyze/providers/` | Two LLM providers behind one interface — **Anthropic** and **OpenAI** — both using schema-enforced structured output, plus a keyword fallback that needs no key at all. |
| `scrape/markdown.py` | HTML → markdown, and the deterministic signals: vendor fingerprints and structural facts, parsed rather than inferred. |
| `scrape/probe.py` | Does a given page actually exist? Guards against sites that answer `200` for URLs that don't. |
| `mapping.py` + `config/mapping.yaml` | Enrichment fields → CRM API names, per destination. No field name is hardcoded anywhere. |
| `config/icp.yaml` | What "good fit" means. Injected into the prompt, so editing it changes every score. |
| `destinations/` | `dryrun`, `hubspot`, `salesforce` — all sharing one find → diff → write upsert. |
| `cli.py` | `check`, `fields`, `scrape`, `probe`, `preview`, `run`, `serve`. |
| `AGENTS.md` + `CLAUDE.md` | The briefing for whatever agent you open this in — bootstrap, adaptation path, guardrails, and the gotchas already paid for. |
| `Dockerfile` + `render.yaml` | One image, three jobs. A blueprint for a web service and a nightly cron, neither deployed. |
| `.github/workflows/` | CI on three Python versions, plus a scheduled-enrichment job — committed deliberately switched off. |

## Requirements

- **Python 3.10+**
- `pip install -e ".[server]"` only if you want `gtm-enrich serve` — FastAPI and
  uvicorn are an extra so the CLI stays light (`.[dev]` already includes them)
- Optionally an **Anthropic** or **OpenAI** key — without one, runs use the keyword fallback
- Optionally a **Firecrawl** / **Apify** key or a local **crawl4ai** container — without one, scraping uses plain HTTP
- Optionally a **HubSpot private app token** or **Salesforce** credentials — without either, writes go to disk

Every backend talks raw HTTP over `httpx`. There is no Firecrawl, crawl4ai, or
Apify SDK to install.

## Quick start

No credentials required — either way.

**Through an agent**, which is what this is designed for. Open the repo and say:

> Read AGENTS.md, then show me this working.

**By hand**, if you'd rather. Needs Python 3.10+, and note that the `python3` on
a stock Mac is 3.9 and carries a pip too old to install this — it fails with
`File "setup.py" or "setup.cfg" not found`, which sounds like a missing file and
is really a stale pip:

```bash
python3.13 -m venv .venv          # or 3.10, 3.11, 3.12
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
.venv/bin/gtm-enrich run --domains examples/domains.csv --dest dryrun
```

Either path scrapes five real homepages, analyzes them, and writes the exact
payloads it *would* send to HubSpot into `out/`. It takes about two seconds.

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
gtm-enrich probe vercel.com              # which pages actually exist?
gtm-enrich preview --source hubspot      # what would this filter pull?
gtm-enrich run --source hubspot --filter config/filters/new-prospects.yaml
gtm-enrich run --domains accounts.csv --provider openai --model gpt-5-nano
gtm-enrich run --domains accounts.csv --scraper apify --dest hubspot
gtm-enrich fields --dest salesforce      # the fields to create before a live run
```

## Getting the list out of the CRM

You should not have to export a CSV to enrich your own database. Write the filter
once, in neutral terms, and it compiles into whatever the target system speaks:

```yaml
fields:
  website:     {hubspot: domain,          salesforce: Website}
  lifecycle:   {hubspot: lifecyclestage,  salesforce: Type}
  enriched_at: {hubspot: gtm_enriched_at, salesforce: GTM_Enriched_At__c}

filters:
  - {field: website,   op: is_known}
  - {field: lifecycle, op: not_in, value: [customer, evangelist]}
  - {field: enriched_at, op: older_than_days, value: 90, or_unknown: true}
```

Becomes SOQL:

```sql
SELECT Id, Name, Website FROM Account
WHERE Website != null AND Type NOT IN ('customer', 'evangelist')
  AND (GTM_Enriched_At__c < 2026-06-12T20:14:45Z OR GTM_Enriched_At__c = null)
```

...and a HubSpot search body with **two** filter groups, because of something
worth knowing: HubSpot ANDs the filters inside a group and ORs between groups,
with no way to express "A AND (B OR C)" directly. So `or_unknown` gets expanded
into a cross product — `(A AND B) OR (A AND C)` — and the shared conditions are
repeated into each group. Each `or_unknown` doubles the group count, which is why
the expansion is capped with an error that explains itself.

That behaviour was measured against a live portal, not assumed: two filters in
one group returned 0 matches, the same two as separate groups returned 720,776.

**The loop closes on itself.** That `enriched_at` clause reads a field this tool
*writes*. "Give me accounts I haven't looked at in 90 days" is a question the
system can answer about its own work, which is what stops a nightly job from
re-enriching everything every night.

**There's an escape hatch,** because any filter language covering two CRMs will
eventually fail to express something real. Drop raw HubSpot JSON or a raw SOQL
`WHERE` clause into the same file and it's used verbatim.

### Preview before you pull

```
$ gtm-enrich preview --source hubspot
New prospects with a website via hubspot

— the query that will run —
POST /crm/v3/objects/companies/search
{ ...the compiled body... }

Preflight: these company properties do not exist in this portal:
gtm_enriched_at. HubSpot rejects the whole query without saying which one.
Run `gtm-enrich fields --dest hubspot` for the list to create.
```

That preflight exists because of what HubSpot actually returns when you reference
a property that isn't there:

```json
{"status": "error", "message": "There was a problem with the request."}
```

No property name, no hint. So the source reads the portal's property list first
and names the missing field itself.

## Running it continuously

Same pipeline, three triggers — see [docs/deployment.md](docs/deployment.md).

**Scheduled.** `--since-last-run` records when each run *started* and asks only
for records modified since. Start rather than finish, so a record changed while a
run was in flight is caught next time instead of falling into the gap. The free
version of this is `.github/workflows/scheduled-enrichment.yml` — Actions on a
cron, with the page and analysis caches persisted between runs — shipped
dispatch-only so a fork never starts spending money by itself.

**Real-time.** `gtm-enrich serve` runs a webhook service that does four things in
this order: **verify, deduplicate, enqueue, return.** The order is the design.

- *Verify* first, so an unauthenticated caller can't fill your queue with paid
  work. HubSpot's v3 signature, with the five-minute replay window enforced.
- *Deduplicate* next, because providers retry deliveries and you should not pay
  twice for the same account.
- *Enqueue* rather than process, because both CRMs want an answer in seconds and
  enrichment takes twenty or more. The queue is SQLite, so it survives a restart
  — an in-memory queue loses everything in flight exactly when you'd notice.

**The gotcha:** records are usually created *empty*, and whatever fills in the
website does so a second later. A naive "on create" trigger sees nothing to
scrape. So the service listens for the website property *changing*, not just the
record appearing, and re-queues a job that finds no website with a delay instead
of dropping it.

## The soft-404 problem

Checking whether a company has a pricing page sounds like a `HEAD /pricing` and a
status code. It isn't. Measured across seven well-known B2B sites, **three
returned `200 OK` for a random 32-character path**:

```
stripe.com    404      linear.app    200   <-- status is useless
gong.io       404      notion.so     200   <-- status is useless
wistia.com    404      vercel.com    200   <-- status is useless
hubspot.com   404
```

SPA catch-all routes and branded "we couldn't find that" pages both do this. A
naive existence check marks every path on those sites as present.

`gtm-enrich probe` guards against it with a **control probe**. Before checking
anything real, it asks the site for a URL that certainly does not exist and keeps
what comes back. That one extra request calibrates every later check against how
*this* site behaves:

```
$ gtm-enrich probe vercel.com
vercel.com answers 200 for a URL that cannot exist — comparing content instead
control page: status 200, 339,314 chars

┏━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Path       ┃ Verdict ┃ Conf ┃ Similarity ┃ Why                           ┃
┡━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ /pricing   │ exists  │ 0.95 │      0.282 │ on-topic content found        │
│ /plans     │ missing │ 0.95 │      0.995 │ soft 404: 1.00 similar to the │
│            │         │      │            │ control page                  │
│ /customers │ exists  │ 0.95 │      0.401 │ on-topic content found        │
└────────────┴─────────┴──────┴────────────┴───────────────────────────────┘
```

Real pages sit near zero similarity to the control, soft 404s near one. The
separation is not marginal — measured on live sites, `/pricing` scored 0.000 and
0.020 while known-missing paths scored 0.926 and 1.000 — so the 0.85 threshold
has enormous headroom in both directions.

Three details make it hold up:

**It adapts to the site.** A site with honest 404s gets its status codes trusted.
A site that 200s everything gets content comparison. A site that 200s everything
*and renders nothing* (a JS shell over plain HTTP) is flagged as such, and
positive verdicts there come back at 0.5 confidence with the reason
`"the control page rendered nothing so soft 404s cannot be ruled out"` — better
to say what you can't prove than to guess.

**"Gated" is a third verdict, not a synonym for missing.** `notion.so/packages`
is neither a real page nor a 404 — it's a login wall saying *"Sign in to see this
page."* Collapsing "we can't tell" into "it isn't there" produces exactly the
confident-and-wrong CRM field this project exists to avoid.

**Markers alone are not enough.** Checking for the words "page not found" catches
the easy case and misses the one that matters: a branded catch-all rendering the
site's normal nav and footer, never admitting anything is wrong. Only the control
comparison catches that, which is why the test suite fixtures one of each.

**One caveat: `probe` is its own command, not a step inside `run`.** The
`gtm_has_pricing_page` and `gtm_has_demo_cta` fields that actually reach the CRM
are read off the homepage's own links — no extra requests, and right most of the
time. Spending a control request plus one request per path on every account, and
writing *that* verdict instead, is the obvious next move and isn't wired up.

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

**[Real output from a real run is committed in `examples/output/`](examples/output/)**
— five accounts scraped through Firecrawl and analyzed by `claude-opus-5`, in both
HubSpot and Salesforce shape, so you can see what one enrichment looks like on the
way into two different systems. Including the account that scored **2 out of 100**
because the model recognized it as a direct competitor.

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
both and `GTM_EFFORT=xhigh` doesn't 400 just because you switched vendor —
Anthropic's `xhigh` and `max` fold down to OpenAI's `high` rather than erroring.
Effort is an environment variable today, not a CLI flag.

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
reports tokens and says there's no estimate, rather than guessing. Cached results
are reported separately — provenance keeps the token counts from the original
call, so a run served from cache says `no API call, no new spend` instead of
billing you twice on paper.

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

- **Dry run is the default.** `--dest dryrun` builds the payload and writes it to
  `out/` instead of sending it, so no CRM is touched until you name a real
  destination. It is not a free rehearsal, though: the scrape and the analysis
  still happen, and still cost whatever your scraper and model cost. What a dry
  run saves you is the bad write, not the bill.
- **Writes are diffed first.** Nothing is sent unless a value would actually change.
- **Credentials live in `.env`**, which is gitignored, or in your shell. Real
  environment variables always beat the file, so CI is never overridden by a
  stale local `.env`.
- **Scraped pages and generated payloads** land in `.cache/` and `out/`, both
  gitignored, so nothing you scrape ends up in git.
- **`GTM_RESPECT_ROBOTS=1` by default.** Turn it off only for sites you own.

## Tests

```bash
pytest -q                  # 216 tests, no network, no credentials
ruff check src tests
```

Every test runs against mocked HTTP transports and stubbed SDK clients, so CI is
hermetic. The suite covers the things most likely to break quietly: robots.txt
refusals, the www fallback, JS-only shells, all four backends' response shapes
(including crawl4ai's three response envelopes, Firecrawl's list-valued metadata,
and a regression test pinning the raw-vs-cleaned HTML choice above), per-destination
type coercion, no-op suppression, `Retry-After`
handling, SOQL injection guards, both providers' refusal paths, token
normalization across vendors, cache invalidation, and every soft-404 verdict
including the branded catch-all that no keyword check would catch, filter
compilation for both query languages, cursor paging on both sources, and every
webhook rejection path — unsigned, tampered, replayed, and redelivered.

## What's proven, and what's left to you

Two different things get called limitations, and they deserve different
treatment: what hasn't been proven, and what was deliberately left for you.

### Not proven

- **`crawl4ai` and `apify` have never made a successful call.** `direct` and
  `firecrawl` are live-tested against real sites, and the Anthropic provider is
  live-tested end to end. The other two backends and the OpenAI provider are
  built to their documented API shapes and covered by tests against mocked
  transports — which, as the raw-vs-cleaned HTML note above shows, is not the
  same as working.
- **No live webhook has ever hit the service.** Every rejection path is tested
  against mocked transports, and the queue survives a restart by design, but
  tests are not traffic. The HubSpot *source* is the exception: filter
  compilation, preflight, and paging were all verified against a real portal.
- **The keyword fallback is genuinely crude.** It's substring matching against a
  keyword list, it's capped at 0.4 confidence, and it labels itself
  `heuristic:v1` in provenance so nobody mistakes it for analysis. It exists so
  the pipeline runs with no API key at all.
- **The Salesforce matcher uses `Website LIKE`,** which is fine for a POC and
  wrong for a large org. [docs/crm-setup.md](docs/crm-setup.md#3-matching-and-why-you-should-change-it)
  explains the external-ID field you'd use instead.

### Left for you, on purpose

This is the public version. The interesting half of this job is the part that
only makes sense once you know the stack it's landing in, so the seams are left
open and opinionated rather than closed and generic:

- **Deployment.** The Dockerfile and the Render blueprint are here and neither
  has been deployed. [docs/deployment.md](docs/deployment.md) argues for what to
  wire where; your infrastructure decides the rest.
- **Scheduling.** `--since-last-run` keeps repeat runs incremental, and both a
  Render cron and a GitHub Actions workflow ship with the repo — the workflow
  deliberately switched off (`workflow_dispatch` only) so a clone never starts
  spending money by itself. Something outside this repo still pulls the trigger.
- **Scope.** The homepage only. No crawling to `/about`, `/pricing`, or
  `/customers`, which is where a lot of the real signal lives — and no product
  usage, no billing data, no support history, all of which beat a homepage for
  anyone already in your funnel. `sources/` and `scrape/fetchers/` are one
  interface each; that is where the next signal goes in.

## Layout

```
src/gtm_enrich/
├── models.py            # typed contract for every stage
├── config.py            # .env + env for secrets, YAML for the ops-editable parts
├── pipeline.py          # orchestration + caching
├── filters.py           # one filter → HubSpot search body or SOQL
├── mapping.py           # enrichment fields → CRM API names
├── state.py             # run watermarks + webhook event dedupe
├── cli.py               # check / fields / scrape / probe / preview / run / serve
├── sources/             # csv · hubspot · salesforce  (where the list comes from)
├── server/              # webhook service · job queue · worker
├── scrape/
│   ├── fetch.py         # robots, apex/www fallback, cache, page assembly
│   ├── markdown.py      # HTML → markdown + deterministic signals
│   ├── probe.py         # does this page exist? soft-404 guard
│   └── fetchers/        # direct · firecrawl · crawl4ai · apify
├── analyze/
│   ├── prompt.py        # the questions, and the cached ICP prefix
│   ├── heuristics.py    # no-key keyword fallback
│   └── providers/       # anthropic · openai
└── destinations/        # base upsert cycle · dryrun · hubspot · salesforce
```

MIT — see [LICENSE](LICENSE).
