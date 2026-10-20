# AFH Product Signal

A static GitHub Pages site (`gh-pages` branch) with a monthly product signal
distilled from App Feedback Hub (AFH) tickets, written for PMs.

## How it works

- Pages are generated and published by the local `/afh-signal` Claude Code skill.
- Before anything is published, the skill runs a PHI scan and waits for explicit
  human approval.
- This repo holds no ticket data and no pipeline code. `main` contains only this
  README and the notify workflow; the site content lives on `gh-pages`.
- By design, the page shows no issue keys and no patient details.

## Notifications

`.github/workflows/notify.yml` posts to Slack via `chat.postMessage` using the `SLACK_BOT_TOKEN` secret (channel set by `SLACK_CHANNEL` in the workflow):

- On a push to `gh-pages` (or manual dispatch) it reads `latest.json`
  (`month_label`, `headline`, `url`, `cards[]`) and posts the month's summary.
  It skips with a notice if `latest.json` or the token is missing.
- On the 2nd of each month (14:00 UTC) it posts a reminder to run
  `/afh-signal month YYYY-MM` for the previous month.

## Publishing a month

Run `/afh-signal month YYYY-MM` locally, review the PHI scan result, approve,
and the skill pushes to `gh-pages`.
