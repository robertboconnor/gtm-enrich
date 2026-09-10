# Running it continuously

Three ways to trigger the same pipeline, in increasing order of setup cost. All
three run identical enrichment code — the trigger is thin on purpose.

> **Nothing here is deployed.** This repo has never had a live webhook pointed at
> it. Everything below is written so you can wire it up yourself, and everything
> is covered by tests against mocked transports — which, as
> [the README notes](../README.md#what-this-isnt), is not the same as proven.

| | Setup | Latency | Needs |
| --- | --- | --- | --- |
| **Manual** | none | you run it | nothing |
| **Scheduled** | minutes | hours | a cron runner, or GitHub Actions |
| **Real-time** | an hour | seconds | a hosted HTTPS service |

---

## 1. Manual

```bash
gtm-enrich run --domains accounts.csv --dest dryrun
gtm-enrich run --source hubspot --filter config/filters/new-prospects.yaml
```

Always available. Adding the other two never takes this away.

---

## 2. Scheduled

### Free: GitHub Actions

[`.github/workflows/scheduled-enrichment.yml`](../.github/workflows/scheduled-enrichment.yml)
is ready but **deliberately disabled** — it only runs when you click it, so a
fork never fires it by accident.

1. In your repo, go to **Settings → Secrets and variables → Actions → New
   repository secret**. Add whichever you use: `ANTHROPIC_API_KEY` or
   `OPENAI_API_KEY`, `FIRECRAWL_API_KEY`, `HUBSPOT_PRIVATE_APP_TOKEN`.
2. Go to the **Actions** tab, pick *Scheduled enrichment*, click **Run
   workflow**. Leave the destination on `dryrun` the first time.
3. When the run finishes, download the `enrichment-results` artifact and read
   what it would have written.
4. Happy with it? Edit the workflow file and uncomment the `schedule:` block.

The workflow caches `.cache/` between runs, so a nightly job only pays for pages
that actually changed.

### Hosted: any container platform

The [`Dockerfile`](../Dockerfile) builds one image that does all three jobs;
which one it does is the command you give it.

```bash
docker build -t gtm-enrich .
docker run --rm --env-file .env -v gtm-cache:/app/.cache gtm-enrich \
  gtm-enrich run --source hubspot --filter config/filters/new-prospects.yaml --since-last-run
```

Point your platform's scheduler at that command.

### Why `--since-last-run` matters

Without it, a nightly job re-reads your entire database every night. With it,
each run records when it *started* and the next one asks only for records
modified since. Start rather than finish, so a record changed while a run was in
flight is caught next time instead of falling into the gap between them.

Belt and braces: the shipped filter also skips anything enriched in the last 90
days, using the `gtm_enriched_at` field this tool writes. The output becomes the
input.

---

## 3. Real-time

### Deploying the service

[`render.yaml`](../render.yaml) defines both a web service and a nightly cron job
from the same image. On Render: **New → Blueprint**, point it at your fork, and
it prompts for each secret — none are stored in the file.

Any platform that runs a container with a public HTTPS URL works the same way.
You need:

- `PORT` — most platforms set this; the service reads it
- A volume at `/app/.cache` — the job queue and run state live there. Without
  one, a restart loses queued jobs and forgets what it has already enriched.
- `GTM_DESTINATION` — **leave it as `dryrun` until you have watched it work.**

Check it came up:

```bash
curl https://your-service.onrender.com/health
```

```json
{"status":"ok","destination":"dryrun","provider":"anthropic",
 "scraper":"firecrawl","queue":{}}
```

### Wiring HubSpot

Two routes. Pick one.

**A workflow webhook** (Operations Hub Professional and up) is the simpler one,
and if you already build HubSpot workflows it is familiar.

1. Create a company-based workflow. Enrollment: *Domain is known* and whatever
   else you want.
2. Add the action **Send a webhook** → **POST** →
   `https://your-service.onrender.com/webhooks/salesforce`.

   Yes, the Salesforce endpoint — it is the shared-secret one, and a HubSpot
   workflow webhook cannot produce HubSpot's request signature. Send a header
   `X-GTM-Secret` matching your `GTM_WEBHOOK_SECRET`, and a body containing the
   record's domain.

**App webhook subscriptions** give you the signed endpoint and true
create/change events.

1. Create a developer app at [app.hubspot.com/developer](https://app.hubspot.com/developer).
2. Under **Webhooks**, set the target URL to
   `https://your-service.onrender.com/webhooks/hubspot`.
3. Subscribe to **`company.creation`** *and* **`company.propertyChange`** on the
   `domain` property. Both, not just creation — see below.
4. Copy the app's **client secret** into `HUBSPOT_CLIENT_SECRET`. The service
   refuses unsigned requests rather than processing them, so this is required.
5. Install the app in your portal.

> HubSpot's webhook setup has moved between private and developer apps over
> time. Check their current docs before you build around either route — this
> describes the shape, not a guarantee about today's UI.

### Wiring Salesforce

A record-triggered Flow is the most legible option.

1. **Setup → Flows → New Flow → Record-Triggered Flow**, object **Account**,
   trigger on **created and updated**, condition `Website is not null`.
2. Add an **HTTP Callout** action to
   `https://your-service.onrender.com/webhooks/salesforce`, `POST`, with header
   `X-GTM-Secret` set to your `GTM_WEBHOOK_SECRET` and a JSON body of
   `{"Id": "{!$Record.Id}", "Website": "{!$Record.Website}"}`.
3. Add the service's domain to **Setup → Remote Site Settings**, or Salesforce
   will block the callout.

### The gotcha nobody warns you about

**Records are usually created empty.** Something creates the company, and
whatever populates its website does so a second or two later. A webhook firing
on creation therefore arrives before there is anything to scrape.

Two things handle it:

- Subscribing to the **property change** on the website field, not just
  creation, so the event that carries a usable value also fires.
- A job that finds no website is **re-queued with a delay** (90 seconds, up to
  three attempts) rather than dropped. The common case resolves itself; genuinely
  website-less records stop costing anything after the third try.

### What the service does with a request

1. **Verify.** HubSpot's v3 signature — HMAC-SHA256 over method + URI + body +
   timestamp, keyed with the client secret, rejected if older than five minutes.
   Unverified requests never reach step 2, so nobody can fill your queue.
2. **Deduplicate.** Every event gets a key, remembered for 72 hours. Providers
   retry; without this you pay for the same enrichment several times.
3. **Enqueue.** Write the job to SQLite and return. Both providers want an answer
   in seconds and enrichment takes twenty or more.
4. **Return 200.** The worker does the rest.

---

## Before you point it at production

- **Keep `GTM_DESTINATION=dryrun`** until you have watched a few real events flow
  through and read what it would have written.
- **Create the custom fields first** — [docs/crm-setup.md](crm-setup.md).
  `gtm-enrich preview` will tell you which are missing before anything runs.
- **Set a limit.** `--limit` on scheduled runs. A filter matching 600,000
  companies is one typo away, and every one of them is an API call and a model
  call.
- **Watch the spend.** Every run prints tokens and estimated cost, and separates
  fresh spend from cache replays.
- **Scale the worker separately if you need to.** The queue's claim is a single
  atomic `UPDATE`, so several workers can drain the same database safely. For
  serious volume, point the same code at Postgres — that is a connection string,
  not a redesign.
