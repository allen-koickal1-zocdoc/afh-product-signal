#!/usr/bin/env python3
"""
notify_slack.py -- post the digest to Slack.

Only aggregates go out: theme labels, counts and the dashboard link. Never a
ticket key, never patient text. Slack is an external destination, so this file
is the last place a PHI leak could happen -- keep it that way.
"""

import argparse
import json
import os
import sys

import requests

SLACK_POST = "https://slack.com/api/chat.postMessage"


def build_blocks(report: dict, base_url: str, cfg: dict, label: str) -> list:
    t = report["totals"]
    themes = report["themes"][:5]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"AFH product signal — {label}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*{t['actionable']}* actionable reports · *{t['blocking']}* cost the patient "
            f"the appointment or money · *{t['noise_pct']}%* of intake was non-actionable."}},
    ]

    if report["regressions"] and cfg.get("include_regressions", True):
        labels = {x["id"]: x["label"] for x in report["themes"]}
        lines = []
        for r in report["regressions"]:
            name = labels.get(r["theme"], r["theme"])
            tail = (f" — concentrated on app {r['app_versions'][0]['version']}, looks release-driven"
                    if r["release_correlated"] else "")
            lines.append(f"• *{name}* — {r['recent_30d']} in 30d vs "
                         f"{r['monthly_baseline']}/mo baseline{tail}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": ":rotating_light: *Active regressions*\n" + "\n".join(lines)}})

    ranked = "\n".join(
        f"{i}. *{x['label']}* ({x['area']}) — {x['total']} reports, {x['blocking']} blocking"
        for i, x in enumerate(themes, 1)
    )
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
                   "text": "*Worst product areas, ranked by patient harm*\n" + ranked}})

    if cfg.get("include_signal_quality", True):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"Signal quality: {t['unclassified_pct']}% unclassified · "
            f"diagnostics on {report['diagnostics']['coverage_pct']}% of tickets"}]})

    blocks.append({"type": "actions", "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": "Open dashboard"},
         "url": base_url, "style": "primary"}]})
    return blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--slack-token", default=os.environ.get("SLACK_BOT_TOKEN"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.report) as fh:
        report = json.load(fh)
    with open(args.config) as fh:
        config = json.load(fh)

    for channel in config["channels"]:
        blocks = build_blocks(report, args.base_url, channel, args.label)
        if args.dry_run:
            print(json.dumps({"channel": channel["name"], "blocks": blocks}, indent=2))
            continue
        if not args.slack_token:
            sys.exit("SLACK_BOT_TOKEN is required (or use --dry-run)")
        resp = requests.post(
            SLACK_POST,
            headers={"Authorization": f"Bearer {args.slack_token}"},
            json={"channel": channel["id"], "blocks": blocks,
                  "text": f"AFH product signal — {args.label}"},
            timeout=30,
        )
        body = resp.json()
        if not body.get("ok"):
            sys.exit(f"Slack post to {channel['name']} failed: {body.get('error')}")
        print(f"posted to {channel['name']}")


if __name__ == "__main__":
    main()
