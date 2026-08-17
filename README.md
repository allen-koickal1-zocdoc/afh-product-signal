# AFH Product Signal

Answers one question every Monday: **what is broken in the Zocdoc app, and which
product area owns it?**

Not a triage report. Nothing here measures the QA team — no close rates, no
time-to-resolution, no per-assignee counts. Every number is about the product.

## What it produces

A single static page, published to GitHub Pages, that ranks product themes by
patient harm rather than by ticket volume:

- **Headline** — the largest single source of patient harm this window.
- **Active regressions** — themes whose last-30-day rate is ≥2× their trailing
  baseline, plus whether the spike sits on one app version (release-driven) or
  is spread across many (backend or content change).
- **Where it hurts** — every theme ranked by impact score, with a severity mix
  bar, month-over-month trend, and the app versions carrying it.
- **By product area** — who owns the fix.
- **Signal quality** — how much of the intake was noise, how much the taxonomy
  failed to classify, and what the feed is blind to.

Plus a Slack digest to `#afh-jira` with the same top five.

## Ranking

Volume is the wrong sort order: a hundred people complaining about dark mode
matter less than eight people who were billed out-of-network. Themes are ranked
by an impact score:

```
impact = 3 × BLOCKS_BOOKING  +  1 × DEGRADES  +  0 × ANNOYANCE
```

`BLOCKS_BOOKING` means the patient lost the appointment or lost money. That is
the fix-first list.

## Pipeline

```
fetch_data.py   Jira REST  ->  tickets.json        (PHI — job-local)
classify.py     tickets    ->  classified.json     (patient-level — job-local)
compute_report  classified ->  report.json         (aggregates — PHI boundary)
assert_no_phi   report.json -> pass/fail           (blocks publish)
generate_pages  report.json -> index.html          (published)
notify_slack    report.json -> Slack               (published)
```

## Classification

`config/taxonomy.json` is the whole classifier: an ordered list of themes, each
with a regex over summary + body + labels. First match wins, so specific themes
sit above general ones.

Deterministic on purpose. No model call in the pipeline, so the same ticket
always lands in the same theme and any number on the dashboard can be traced
back to a line of config. The cost is a tail the regexes miss — that tail is
published as the **unclassified %**. A rising unclassified rate means the
taxonomy is stale, not that the queue is quiet. Retune by reading the
unclassified bodies and widening a `match`; never by deleting the bucket.

Adding a theme:

```jsonc
{
  "id": "kebab-case-id",
  "label": "What a PM would call it",
  "area": "Search & Discovery",          // must exist in `areas`
  "why": "One sentence on the patient impact.",
  "severity_floor": "BLOCKS_BOOKING",    // optional; only if it reliably costs the appointment
  "match": "regex|over|summary body and labels"
}
```

## PHI

AFH tickets contain patient free text plus device, session and tracking IDs.
One ticket is one patient, so **a ticket key is patient-level data** — keys never
reach the dashboard or Slack.

- `fetch_data.py` strips `tracking_id` / `session_id` / `device_id` on parse.
- `classify.py` emits no free text — only theme, type, severity, version.
- `compute_report.py` is the boundary: counts and labels only.
- `assert_no_phi.py` runs before publish and fails the build on any ticket key,
  UUID, email, phone number or banned field name in `report.json`.
- The workflow writes PHI only to `/tmp/afh` and shreds it in an `always()` step.
- `.gitignore` blocks `tickets.json`, `classified.json` and `work/`.

Drill-down into individual tickets happens in Jira, which is permission-gated.
The dashboard links to a JQL query, not to data.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export JIRA_EMAIL=you@zocdoc.com JIRA_API_TOKEN=...

.venv/bin/python scripts/fetch_data.py --start-date 2025-12-01 --end-date $(date +%F) --output-dir work
.venv/bin/python scripts/classify.py --tickets work/tickets.json --output work/classified.json
.venv/bin/python scripts/compute_report.py --classified work/classified.json --output work/report.json \
  --start-date 2025-12-01 --end-date $(date +%F)
.venv/bin/python scripts/assert_no_phi.py --report work/report.json
.venv/bin/python scripts/generate_pages.py --report work/report.json --output-dir work/pages
open work/pages/index.html
```

Slack digest without posting: add `--dry-run` to `notify_slack.py`.

## Secrets

| Secret | Purpose |
|---|---|
| `JIRA_EMAIL` | Jira account email for API auth |
| `JIRA_API_TOKEN` | Jira API token |
| `SLACK_BOT_TOKEN` | Bot token for the `#afh-jira` digest |

## Schedule

Mondays 11:00 UTC. Manual runs: Actions → "AFH Product Signal" → Run workflow,
optionally overriding the date window or skipping Slack.

## Known data quirks

- JQL `key >= AFH-n` returns **false zeroes** on this project. Always window by
  `created`, never by key range.
- Tickets get moved off AFH onto team boards and still resolve by their AFH key,
  so `project = AFH` undercounts. `fetch_data.py` sweeps the known destination
  boards and warns — those moves also carry patient text onto boards with wider
  visibility, which is a containment gap worth closing.
- `AFH-701`..`AFH-718` are synthetic records from a skill demo on 2026-07-27.
  Hard-excluded in `config.py`.
- Diagnostics (app version / OS / device) only exist from **April 2026** onward.
  Version correlation before that date is meaningless.
- Android submissions almost never carry diagnostics, so Android quality is
  effectively unmeasured by this feed. Treat the platform split as iOS-only.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```
