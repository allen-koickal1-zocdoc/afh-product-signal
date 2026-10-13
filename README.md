# AFH Product Signal

Answers one question every month: **what is broken in the Zocdoc app, and which
product area owns it?**

Not a triage report. Nothing here measures the QA team — no close rates, no
time-to-resolution, no per-assignee counts. Every number is about the product.

## What it produces

A static site published to GitHub Pages, ranking product themes by patient harm
rather than by ticket volume.

**`index.html`** — the overview:

- **Headline** — the largest single source of patient harm this window.
- **Active regressions** — themes whose last-30-day rate is ≥2× their trailing
  baseline, plus whether the spike sits on one app version (release-driven) or
  is spread across many (backend or content change).
- **New normals** — themes that stepped up to a new level rather than spiking,
  which is the shape a phased rollout produces.
- **Where it hurts** — every theme ranked by impact score, with a severity mix
  bar, sparkline, MoM trend, its top sub-themes, and the app versions carrying it.
- **Movers** — biggest MoM swings, ranked by ratio rather than raw delta.
- **First seen this month** — themes with no history to compare against.
- **By product area** — who owns the fix, with each area's themes listed.
- **Signal quality** — how much intake was noise, how much the taxonomy failed
  to classify, and what the feed is blind to.

**`themes/<id>.html`** — one page per theme:

- Sub-theme breakdown: *what specifically* is failing inside the theme, with an
  explicit coverage figure so you know how much of it the breakdown explains.
- Month-by-month table separating blocking from non-blocking.
- Full app-version distribution, platform split, defect/request/dissatisfaction mix.
- First seen, last seen, months active, share of all blocking feedback.

**Exports** — `report.json`, `themes.csv`, `subthemes.csv`, `monthly.csv`
(theme × month, long format). Linked from the page footer. All aggregates.

Plus a Slack digest to `#test-ak` with the top five.

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
  "match": "regex|over|summary body and labels",
  "subthemes": [                         // optional second pass, first match wins
    { "id": "kebab-id", "label": "What specifically failed", "match": "regex" }
  ]
}
```

### Sub-themes

A theme answers *which product area*; a sub-theme answers *what specifically*.
Same mechanism — an ordered regex list, first match wins — run only against
tickets that already matched the parent theme.

Anything the parent matched but no sub-theme did is reported as **Other**, and
each theme page prints its sub-theme coverage. Below 50% the page says outright
that the split is a hint rather than a finding. **Never pad sub-themes to shrink
Other** — a large Other bucket is telling you the theme label is broader than it
claims, which is worth more than a tidy pie chart.

To author them, use the phrase miner rather than reading ticket bodies:

```bash
python3 scripts/mine_phrases.py --workdir work                       # theme counts
python3 scripts/mine_phrases.py --workdir work --theme <theme-id>    # recurring phrases
python3 scripts/mine_phrases.py --workdir work --unclassified        # the coverage gap
```

It is a local authoring aid, not part of the pipeline. It counts by distinct
ticket and hides anything appearing in fewer than `--min-tickets`, so you see
product vocabulary rather than one patient's words. Its output is **not**
publishable.

## PHI

AFH tickets contain patient free text plus device, session and tracking IDs.
One ticket is one patient, so **a ticket key is patient-level data** — keys never
reach the dashboard or Slack.

- `fetch_data.py` strips `tracking_id` / `session_id` / `device_id` on parse.
- `classify.py` emits no free text — only theme, type, severity, version.
- `compute_report.py` is the boundary: counts and labels only.
- `assert_no_phi.py` runs twice. Once structurally over `report.json`
  (banned field names at any depth), then again over **every file about to be
  served** — HTML, CSV, JSON — because a template or an export can leak
  something the aggregate never held. Either failure blocks the deploy.
- The workflow writes PHI only to `/tmp/afh` and shreds it in an `always()` step.
  The theme→ticket-key audit map is never written on the runner; build it locally with `run_month.sh`.
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
.venv/bin/python scripts/assert_no_phi.py --report work/report.json --pages-dir work/pages
open work/pages/index.html
```

Slack digest without posting: add `--dry-run` to `notify_slack.py`.

## Secrets

| Secret | Purpose |
|---|---|
| `JIRA_EMAIL` | Jira account email for API auth |
| `JIRA_API_TOKEN` | Jira API token |
| `SLACK_BOT_TOKEN` | Bot token for the `#test-ak` digest |
| `AFH_GOLD_SET` | Contents of `tests/data/gold_set.tsv` (train labels; ticket keys, so never committed) |
| `AFH_TEST_SET` | Contents of `tests/data/test_set.tsv` (held-out labels; same) |

## Schedule

**09:00 UTC on the 2nd of every month**, covering the month that just closed.
The 2nd rather than the 1st so late-arriving tickets are counted.

Manual runs: **Actions → AFH Product Signal → Run workflow**. Two inputs:

| Input | Effect |
|---|---|
| `month` | `YYYY-MM`. Blank = last complete month. Give it the current month for a month-to-date read; the window clamps to today and the page is labelled *(month to date)*. |
| `skip_slack` | Regenerate the dashboard without posting to `#test-ak`. Use this when re-running after a taxonomy change. |

Backfilling an older month is the same thing: pass its `YYYY-MM`.

### What "monthly" actually means for the numbers

Only the **end** of the window moves. `start` is pinned to `FIRST_DAY`, because
the trend column, the sparklines and the level-shift detector all need the whole
history — a report that only saw one month could not tell a new problem from an
old one. See `scripts/resolve_window.py`.

Ending on the last day of a complete month (rather than "today") is what makes
the month-over-month column trustworthy. A run that ended today would compare a
partial month against a complete one and read negative every single time.

### Archive

Each run publishes twice:

- `https://<org>.github.io/<repo>/` — always the newest month
- `https://<org>.github.io/<repo>/archive/2026-08/` — that month, frozen

The Slack digest links to the **permalink**, so a message posted in June still
shows June's numbers a year later. Old months are never overwritten
(`publish.py` clones gh-pages and only replaces the month it is publishing).

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
