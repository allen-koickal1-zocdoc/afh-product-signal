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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    with open(args.report) as fh:
        report = json.load(fh)

    problems = walk(report)
    if problems:
        print("PHI guard FAILED — refusing to publish:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)

    print("PHI guard passed: report contains aggregates only")


if __name__ == "__main__":
    main()
