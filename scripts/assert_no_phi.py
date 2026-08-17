#!/usr/bin/env python3
"""
assert_no_phi.py -- fail the build if anything patient-level reached report.json.

report.json is the only file that gets published. This is a structural check,
not a content scanner: it asserts the shape of the artefact rather than trying
to recognise patient text, because recognising patient text reliably is not
something a regex can do.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Anything shaped like a ticket key, a UUID, an email, a phone number or a
# long opaque identifier has no business in an aggregate report.
FORBIDDEN = {
    "jira ticket key": re.compile(r"\b(AFH|GROAR|GRO|PINATA|SCHED|REL)-\d+\b"),
    "uuid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    "email address": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "phone number": re.compile(r"\b(\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"),
    "long opaque id": re.compile(r"\b[0-9a-f]{24,}\b", re.I),
}

# Keys whose values are known-safe URLs built by compute_report.py. JQL strings
# legitimately contain "project = AFH" and date literals.
ALLOWED_KEYS = {"jira"}

# Fields that must never appear anywhere in the report, at any depth.
BANNED_FIELDS = {"key", "keys", "body", "summary", "description", "reporter",
                 "reporter_email", "assignee", "device", "tracking_id",
                 "session_id", "device_id", "patient", "email"}


def walk(node, path=""):
    problems = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if k in ALLOWED_KEYS:
                continue
            if k.lower() in BANNED_FIELDS:
                problems.append(f"{here}: banned field name '{k}'")
                continue
            problems.extend(walk(v, here))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            problems.extend(walk(v, f"{path}[{i}]"))
    elif isinstance(node, str):
        for name, pat in FORBIDDEN.items():
            if pat.search(node):
                problems.append(f"{path}: contains {name}")
    return problems


def scan_text(path: Path):
    """
    Flat scan of a rendered file.

    walk() checks the shape of report.json, but the dashboard now also emits
    HTML and CSVs, and those are what actually get served. A file that never
    goes through walk() has never been checked, so scan every published byte.
    """
    text = path.read_text(errors="replace")
    problems = []
    for name, pat in FORBIDDEN.items():
        for hit in pat.findall(text):
            hit = hit if isinstance(hit, str) else next((h for h in hit if h), "")
            # The JQL links legitimately contain "project = AFH" and dates, but
            # never a numbered key, which is what the pattern actually matches.
            problems.append(f"{path.name}: contains {name} ({hit[:40]!r})")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", help="report.json — structural check")
    ap.add_argument("--pages-dir", help="published output — flat text scan of every file")
    args = ap.parse_args()
    if not args.report and not args.pages_dir:
        sys.exit("give --report, --pages-dir, or both")

    problems, checked = [], 0

    if args.report:
        with open(args.report) as fh:
            problems += walk(json.load(fh))
        checked += 1

    if args.pages_dir:
        for path in sorted(Path(args.pages_dir).rglob("*")):
            if path.is_file() and path.suffix in {".html", ".csv", ".json", ".txt", ".md"}:
                problems += scan_text(path)
                checked += 1

    if problems:
        print("PHI guard FAILED — refusing to publish:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)

    print(f"PHI guard passed: {checked} file(s), aggregates only")


if __name__ == "__main__":
    main()
