#!/usr/bin/env bash
#
# run_month.sh -- generate and publish the monthly report from this machine.
#
# Every step that can see patient free text runs locally. Jira credentials stay
# in the local environment, the ticket bodies stay in a workdir that is shredded
# on exit, and the only thing that ever reaches GitHub is the aggregate site --
# which has already passed the PHI guard twice by the time it is pushed.
#
#   ./scripts/run_month.sh                 # last complete month, dry run
#   ./scripts/run_month.sh 2026-07         # a specific month, dry run
#   ./scripts/run_month.sh 2026-07 --push  # same, and publish it
#
# Requires JIRA_EMAIL and JIRA_API_TOKEN in the environment.

set -euo pipefail

cd "$(dirname "$0")/.."

MONTH="${1:-}"
[[ "${MONTH}" == --* ]] && MONTH=""
PUSH=""
for arg in "$@"; do [[ "${arg}" == "--push" ]] && PUSH="--push"; done

FIRST_DAY="2025-12-01"          # date of the first AFH ticket
WORKDIR="$(mktemp -d -t afh)"   # PHI lives here, and only here
PY="${PY:-.venv/bin/python3}"

# Shred on any exit path, including failure and Ctrl-C. The workdir holds raw
# patient text; leaving it behind after a crashed run is the whole risk.
cleanup() { rm -rf "${WORKDIR}"; }
trap cleanup EXIT

if [[ -z "${JIRA_EMAIL:-}" || -z "${JIRA_API_TOKEN:-}" ]]; then
  echo "JIRA_EMAIL and JIRA_API_TOKEN must be set" >&2
  exit 1
fi

step() { printf '\n=== %s\n' "$1"; }

step "Tests"
"${PY}" -m pytest tests/ -q

step "Resolve window"
while IFS="=" read -r k v; do printf -v "$k" "%s" "$v"; done < <("${PY}" scripts/resolve_window.py --month "${MONTH}" --first-day "${FIRST_DAY}")
echo "window ${start} -> ${end}  (${label})"

step "Fetch tickets (PHI — local only)"
"${PY}" scripts/fetch_data.py \
  --start-date "${start}" --end-date "${end}" --output-dir "${WORKDIR}"

step "Classify"
"${PY}" scripts/classify.py \
  --tickets "${WORKDIR}/tickets.json" --output "${WORKDIR}/classified.json"

step "Grouping precision"
AFH_WORKDIR="${WORKDIR}" "${PY}" -m pytest tests/test_precision.py -q

step "Compute report — the PHI boundary"
"${PY}" scripts/compute_report.py \
  --classified "${WORKDIR}/classified.json" \
  --output "${WORKDIR}/report.json" \
  --start-date "${start}" --end-date "${end}" \
  --audit-output "${WORKDIR}/audit.json"

step "PHI guard on the aggregate"
"${PY}" scripts/assert_no_phi.py --report "${WORKDIR}/report.json"

step "Generate pages"
"${PY}" scripts/generate_pages.py \
  --report "${WORKDIR}/report.json" --output-dir "${WORKDIR}/pages"

step "PHI guard on everything about to be published"
"${PY}" scripts/assert_no_phi.py \
  --report "${WORKDIR}/report.json" --pages-dir "${WORKDIR}/pages"

step "Keep the audit trail"
# The workdir is shredded on exit, so the theme -> ticket-key map has to be
# copied out or the month's traceability dies with it. .audit/ is gitignored and
# never published: it holds Jira keys, which the PHI guard bans from the site.
mkdir -p .audit
cp "${WORKDIR}/audit.json" ".audit/${month}.json"
echo "wrote .audit/${month}.json (ticket keys — local only, never publish)"

step "Publish"
# --keep leaves an inspectable copy behind precisely because the workdir does
# not survive this script; without it a dry run would leave nothing to look at.
"${PY}" scripts/publish.py \
  --pages "${WORKDIR}/pages" \
  --report "${WORKDIR}/report.json" \
  --month "${month}" --label "${label}" \
  --keep "$(pwd)/.site-preview" \
  ${PUSH}

step "Done"
echo "preview: .site-preview/index.html"
if [[ -z "${PUSH}" ]]; then
  echo "nothing was pushed. Re-run with --push once the preview looks right."
fi
echo "audit trail: .audit/${month}.json — traces every published number to its tickets."
