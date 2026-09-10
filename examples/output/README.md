# Example output

Real output from a real run, committed so you can see what this produces without
cloning it. Regenerate with:

```bash
gtm-enrich run --domains examples/domains.csv --scraper firecrawl --dest dryrun
```

| File | What it is |
| --- | --- |
| [`payloads-hubspot.json`](payloads-hubspot.json) | The exact payloads the HubSpot destination would POST — `companies` objects, matched on `domain`. |
| [`payloads-salesforce.json`](payloads-salesforce.json) | The same five records shaped for Salesforce — `Account` objects with `__c` field names, matched on `Website`. |
| [`records-hubspot.csv`](records-hubspot.csv) | The same data flattened, one row per account, for anyone who'd rather open a spreadsheet. |
| [`full-results.json`](full-results.json) | Everything: analysis, provenance, page signals, and the per-row write outcomes. |

Putting the two payload files side by side is the point. Same run, same
enrichment, different field names and different types — `"true"` as a string for
a HubSpot checkbox, `true` as a JSON boolean for a Salesforce checkbox — with no
code change between them. That difference comes entirely from
[`config/mapping.yaml`](../../config/mapping.yaml).

## About these particular numbers

**These were produced by the keyword fallback, not by a model.** Every record
says so: `"gtm_enrichment_source": "heuristic:v1"`, and confidence is pinned at
`0.4`. The rationale field admits it in plain language —

> "Heuristic keyword match only -- no model was used. 4 good-fit and 0 poor-fit
> ICP terms found on the page."

So read these files for the *shape* — the fields, the types, the provenance, the
per-destination mapping — and not as an example of good analysis. With
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` set, the same run fills the same fields
with real judgement: a considered category instead of `crm` for a revenue
intelligence product, a resolved `gtm_segment` instead of `Unclear`, and an ICP
rationale that cites the page.

The deterministic parts are identical either way, because no model touches them:
`gtm_tech_signals` and the `gtm_has_*` flags are parsed from the HTML, and the
provenance fields are bookkeeping.

Pages were fetched through the `firecrawl` backend on 2026-09-10. All five are
public company homepages.
