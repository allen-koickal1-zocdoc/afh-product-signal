#!/usr/bin/env python3
"""
fetch_data.py -- pull AFH tickets from Jira into a local work directory.

The output of this step CONTAINS PHI (patient free text, device / session /
tracking IDs). It is written to --output-dir, which must never be committed or
published. Only the aggregates produced by compute_report.py leave this machine.
"""

import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(__file__))
from config import (  # noqa: E402
    JIRA_REST_URL,
    AFH_PROJECT_KEY,
    MOVED_TO_PROJECTS,
    APP_INFO_MARKERS,
    SYNTHETIC_KEYS,
)

PAGE_SIZE = 100
FIELDS = ["summary", "description", "status", "labels", "assignee", "created", "resolutiondate"]


def _adf_to_text(node) -> str:
    """Flatten an Atlassian Document Format body to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") in ("hardBreak", "paragraph"):
        return _adf_to_text(node.get("content")) + "\n"
    return _adf_to_text(node.get("content"))


def split_app_info(body: str):
    """
    Separate the app-appended diagnostics block from the human-written text.

    Returns (human_text, app_info_dict). The app-info block only started
    appearing in April 2026, so it is absent on most historical tickets.
    """
    lines = body.splitlines()
    cut = None
    for i, line in enumerate(lines):
        if any(m.lower() in line.lower() for m in APP_INFO_MARKERS):
            cut = i
            break
    if cut is None:
        return body.strip(), {}

    human = "\n".join(lines[:cut]).strip()
    info = {}
    for line in lines[cut:]:
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip().lower().replace(" ", "_")] = v.strip()
    # Patient-linkable identifiers never leave this function.
    for drop in ("tracking_id", "session_id", "device_id", "user_id"):
        info.pop(drop, None)
    return human, info


def parse_issue(issue: dict) -> dict:
    fields = issue.get("fields", {})
    body = _adf_to_text(fields.get("description"))
    human, app_info = split_app_info(body)
    assignee = fields.get("assignee") or {}
    key = issue.get("key", "")
    return {
        "key": key,
        "summary": fields.get("summary", ""),
        "body": human,
        "status": (fields.get("status") or {}).get("name", ""),
        "labels": fields.get("labels") or [],
        "assignee": assignee.get("displayName", ""),
        "created": (fields.get("created") or "")[:10],
        "resolved": (fields.get("resolutiondate") or "")[:10],
        "app_version": app_info.get("app_version", ""),
        "os": app_info.get("os_version", "") or app_info.get("os", ""),
        "device": app_info.get("device_model", ""),
        "has_diagnostics": bool(app_info),
        "is_synthetic": key in SYNTHETIC_KEYS,
    }


def search(session, jql: str) -> list:
    """Page through a JQL search. Retries transient 429/5xx.

    /search/jql pages by token; the old /search (startAt + total) is retired.
    """
    out, token = [], None
    while True:
        body = {"jql": jql, "maxResults": PAGE_SIZE, "fields": FIELDS}
        if token:
            body["nextPageToken"] = token
        for attempt in range(4):
            resp = session.post(f"{JIRA_REST_URL}/search/jql", json=body, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            if not resp.ok:
                print(f"Jira search error {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
            resp.raise_for_status()
            break
        else:
            raise RuntimeError(f"Jira search failed after retries: {jql}")

        data = resp.json()
        issues = data.get("issues", [])
        out.extend(parse_issue(i) for i in issues)
        token = data.get("nextPageToken")
        if not token or not issues:
            return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-date", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end-date", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--jira-email", default=os.environ.get("JIRA_EMAIL"))
    ap.add_argument("--jira-token", default=os.environ.get("JIRA_API_TOKEN"))
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args()

    if not args.jira_email or not args.jira_token:
        sys.exit("JIRA_EMAIL and JIRA_API_TOKEN are required")

    session = requests.Session()
    session.auth = (args.jira_email, args.jira_token)
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    window = f'created >= "{args.start_date} 00:00" AND created <= "{args.end_date} 23:59"'

    # NOTE: JQL `key >= AFH-n` returns false zeroes on this project. Always
    # window by created date, never by key range.
    tickets = search(session, f"project = {AFH_PROJECT_KEY} AND {window} ORDER BY created ASC")

    moved_projects = ", ".join(MOVED_TO_PROJECTS)
    moved = search(
        session,
        f"project IN ({moved_projects}) AND {window} AND "
        f'(labels = "afh-issue" OR summary ~ "AFH-") ORDER BY created ASC',
    )

    os.makedirs(args.output_dir, exist_ok=True)
    with open(os.path.join(args.output_dir, "tickets.json"), "w") as fh:
        json.dump(tickets, fh, indent=2)
    with open(os.path.join(args.output_dir, "moved_tickets.json"), "w") as fh:
        json.dump(moved, fh, indent=2)

    print(f"fetched {len(tickets)} AFH tickets, {len(moved)} moved tickets -> {args.output_dir}")
    if moved:
        print(
            f"WARNING: {len(moved)} tickets carrying AFH patient text now live on "
            f"team boards ({moved_projects}). AFH originals are not supposed to be moved."
        )


if __name__ == "__main__":
    main()
