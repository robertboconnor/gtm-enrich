# CRM setup

The pipeline writes into fields that have to exist first. This is the part that
is genuinely fiddly in both systems, so it is written out step by step.

Run this to see exactly which fields your current `config/mapping.yaml` expects:

```bash
gtm-enrich fields --dest hubspot
gtm-enrich fields --dest salesforce
```

Nothing here is needed for a dry run.

---

## HubSpot

### 1. Create a private app

Private apps replaced HubSpot's old API keys. In HubSpot, go to **Settings** (the
gear icon, top right) → **Integrations** → **Private Apps** → **Create a private
app**. On the **Scopes** tab, tick:

- `crm.objects.companies.read`
- `crm.objects.companies.write`

Create the app, then copy the access token it shows you. Put it in `.env` as
`HUBSPOT_PRIVATE_APP_TOKEN`. The token is shown once — if you lose it, rotate it
from the same screen.

### 2. Create the company properties

**Settings** → **Data Management** → **Properties** → **Create property**, with
**Object type: Company**. Create one property per row of `gtm-enrich fields
--dest hubspot`, using the API name from that table.

The mapping's `type` column maps onto HubSpot field types like this:

| mapping type | HubSpot field type            |
| ------------ | ----------------------------- |
| `string`     | Single-line text              |
| `list`       | Multi-line text               |
| `number`     | Number                        |
| `boolean`    | Single checkbox               |
| `datetime`   | Date picker                   |

`gtm_icp_fit_rationale` and `gtm_one_liner` should be multi-line text — they run
long and a single-line property will truncate them.

> HubSpot derives the internal API name from the label you type, so a label of
> "GTM ICP Fit Score" becomes `gtm_icp_fit_score`. Check the internal name on the
> property before saving; if it does not match the mapping, edit `mapping.yaml`
> rather than fighting HubSpot.

### 3. Matching

Records are matched on the standard `domain` property, which HubSpot already
populates and dedupes on. Nothing extra to configure.

---

## Salesforce

### 1. Get credentials

Two supported routes.

**Session token (fastest, for a one-off test).** From a logged-in org you can
pull an access token and instance URL out of a browser session or the Salesforce
CLI (`sf org display --json`). Set `SF_INSTANCE_URL` and `SF_ACCESS_TOKEN`. These
expire — fine for a demo, not for anything scheduled.

**Client credentials flow (what you would actually run).** In Setup, go to **App
Manager** → **New Connected App** → enable **OAuth Settings**, tick *Enable Client
Credentials Flow*, and set a run-as user with permission to edit Accounts. Copy
the consumer key and secret into `SF_CLIENT_ID` and `SF_CLIENT_SECRET`. For a
sandbox, also set `SF_LOGIN_URL=https://test.salesforce.com`.

### 2. Create the custom fields

**Setup** → **Object Manager** → **Account** → **Fields & Relationships** → **New**,
once per row of `gtm-enrich fields --dest salesforce`.

| mapping type | Salesforce field type                                    |
| ------------ | -------------------------------------------------------- |
| `string`     | Text (255), or Text Area (Long) for the rationale         |
| `list`       | Text Area (Long)                                          |
| `number`     | Number (3, 0) for the score; Number (3, 2) for confidence |
| `boolean`    | Checkbox                                                  |
| `datetime`   | Date/Time                                                 |

Every custom field name ends in `__c`. Salesforce adds that suffix for you.

Then grant field-level security on each field to the profile your integration
user runs as — a field the API user cannot see will silently not be written.

### 3. Matching, and why you should change it

By default this matches on the standard `Website` field:

```sql
SELECT Id, ... FROM Account WHERE Website LIKE '%acme.com%' LIMIT 1
```

That works, but it has two real problems: `LIKE` on an unindexed text field gets
slow on a large org, and `%acme.com%` will happily match `notacme.com.au`.

The production answer is a dedicated external-ID field:

1. Create a `Domain__c` field on Account: **Text(255)**, **External ID**, **Unique**.
2. Backfill it with the normalized domain for existing accounts.
3. Change the Salesforce `match_keys` entry in `config/mapping.yaml` to `Domain__c`,
   and add a `salesforce: Domain__c` row mapped from `domain`.

That gives you an indexed, exact match, and it is also the prerequisite for
using Salesforce's native `PATCH /sobjects/Account/Domain__c/{value}` upsert
endpoint if you later want to replace the find-then-write cycle with one call.

---

## Verify before you write

```bash
# See the exact payload, with real field names, without touching an org.
gtm-enrich run --domains examples/domains.csv --dest dryrun --shape salesforce
```

Then open `out/payloads-salesforce-*.json`. If a field name in there does not
exist in your org, the live run will fail on that record with the API's own
error message and continue to the next one.
