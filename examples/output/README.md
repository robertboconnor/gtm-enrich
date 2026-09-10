# Example output

Real output from a real run, committed so you can see what this produces without
cloning it. Pages fetched through the `firecrawl` backend, analyzed by
`claude-opus-5`, on 2026-09-10. Regenerate with:

```bash
gtm-enrich run --domains examples/domains.csv --scraper firecrawl --dest dryrun
```

| File | What it is |
| --- | --- |
| [`payloads-hubspot.json`](payloads-hubspot.json) | The exact payloads the HubSpot destination would POST — `companies` objects, matched on `domain`. |
| [`payloads-salesforce.json`](payloads-salesforce.json) | The same five records shaped for Salesforce — `Account` objects with `__c` field names, matched on `Website`. |
| [`records-hubspot.csv`](records-hubspot.csv) | The same data flattened, one row per account, for anyone who'd rather open a spreadsheet. |
| [`full-results.json`](full-results.json) | Everything: analysis, provenance, page signals, supporting quotes, and the per-row write outcomes. |

Putting the two payload files side by side is the point. Same run, same
enrichment, different field names and different types — `"true"` as a string for
a HubSpot checkbox, `true` as a JSON boolean for a Salesforce checkbox — with no
code change between them. That difference comes entirely from
[`config/mapping.yaml`](../../config/mapping.yaml).

## What the scores look like

| Domain | ICP | Confidence | Category |
| --- | ---: | ---: | --- |
| stripe.com | 58 | 0.86 | payments / financial infrastructure |
| linear.app | 72 | 0.82 | product development / issue tracking |
| gong.io | 72 | 0.85 | revenue intelligence / sales AI software |
| **wistia.com** | **2** | **0.97** | video hosting / video marketing platform |
| notion.so | 62 | 0.85 | productivity / collaboration software |

The ICP in [`config/icp.yaml`](../../config/icp.yaml) is a B2B video platform, and
it lists "video hosting or video platform competitor" as a poor-fit signal.
Wistia scores **2 out of 100 with the highest confidence in the batch**, and its
`gtm_buying_signals` is empty — the model declined to hand a rep talking points
for an account it had just disqualified. The rationale says why:

> "Wistia is a direct competitor — it sells exactly the video hosting, analytics,
> and lead-capture platform we sell, including HubSpot/Marketo/Pardot
> integrations. Automatic disqualification regardless of otherwise strong
> content-marketing signals."

Gong is the opposite case: a strong 72, capped rather than higher because the
copy skews enterprise and the homepage shows no embedded marketing video. Its
buying signals are specific enough to act on — a named user conference with
dates, three named product launches, a co-produced webinar.

Every field is schema-enforced, so none of this needed cleanup before it was
CRM-ready.

## Reproducing it costs about fifty cents

The five analyses came to 70,913 tokens, roughly $0.43 at `claude-opus-5` list
price. For bulk work, `--model claude-haiku-4-5` or `--provider openai --model
gpt-5-nano` costs one to two orders of magnitude less; opus is here because this
is a five-account demo, not a nightly job.

Re-running is free — analyses are cached against page content, model, and ICP, so
a second run reports `5 of 5 served from the analysis cache — no API call, no new
spend`.

## For comparison, without a key

With no `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, the same command still runs and
fills the same fields with the keyword fallback. It is visibly worse and says so:
`"gtm_enrichment_source": "heuristic:v1"`, confidence pinned at `0.4`, gong.io
categorized as `crm` instead of revenue intelligence, segment `Unclear`, and
Wistia scored 42 rather than disqualified.

The deterministic fields are identical either way, because no model touches them:
`gtm_tech_signals` and the `gtm_has_*` flags are parsed from the HTML.

All five are public company homepages.
