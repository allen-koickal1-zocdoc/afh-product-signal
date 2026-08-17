#!/usr/bin/env python3
"""
compute_report.py -- aggregate classified rows into the published report.

This is the PHI boundary. Input rows are patient-level (one AFH ticket is one
patient). Everything written to report.json is a count, a rate or a theme label.
No ticket keys, no dates-per-ticket, no free text.
"""

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from config import JIRA_SEARCH_URL, load_taxonomy  # noqa: E402

# A theme counts as a live regression if the trailing 30 days carry at least
# this many reports AND that rate is this many times the preceding baseline.
REGRESSION_MIN_COUNT = 4
REGRESSION_MULTIPLIER = 2.0

# A theme that barely existed before the last 90 days and is now steady is a
# level shift, not a spike -- the slow shape a phased rollout produces.
LEVEL_SHIFT_MIN_COUNT = 6
LEVEL_SHIFT_MAX_PRIOR = 2

IMPACT_WEIGHTS = {"BLOCKS_BOOKING": 3, "DEGRADES": 1, "ANNOYANCE": 0, "NONE": 0}


def _month(iso: str) -> str:
    return iso[:7] if iso else "unknown"


def _jql_link(jql: str) -> str:
    from urllib.parse import quote

    return JIRA_SEARCH_URL + quote(jql, safe="")


def detect_level_shifts(rows, as_of: date):
    """
    Catch the regressions a 30-day window cannot see.

    A gradual rollout does not spike -- it steps up to a new normal and stays
    there, so by the time it is 30 days old the trailing baseline has already
    absorbed it. This looks for themes that barely existed before a point in
    time and have been steady since: near-zero for the first half of the
    window, concentrated in the last 90 days.
    """
    shift_cut = as_of - timedelta(days=90)
    recent, prior = Counter(), Counter()
    versions = defaultdict(Counter)

    for r in rows:
        if r["type"] == "NON_ACTIONABLE" or not r["created"]:
            continue
        try:
            d = datetime.strptime(r["created"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d > shift_cut:
            recent[r["theme"]] += 1
            if r["app_version"]:
                versions[r["theme"]][r["app_version"]] += 1
        else:
            prior[r["theme"]] += 1

    out = []
    for theme, n in recent.most_common():
        if n < LEVEL_SHIFT_MIN_COUNT:
            continue
        before = prior[theme]
        if before > LEVEL_SHIFT_MAX_PRIOR:
            continue
        top_versions = versions[theme].most_common(3)
        out.append({
            "theme": theme,
            "recent_90d": n,
            "prior_total": before,
            "verdict": "emerged",
            "app_versions": [{"version": v, "count": c} for v, c in top_versions],
            "release_correlated": bool(top_versions) and top_versions[0][1] >= 0.5 * n,
        })
    return out


def detect_regressions(rows, as_of: date):
    """
    Flag themes whose recent rate has jumped, and report the app versions the
    spike sits on. A spike concentrated on one or two versions is a release
    regression; a spike spread evenly across versions is usually a backend or
    content change.
    """
    recent_cut = as_of - timedelta(days=30)
    baseline_cut = as_of - timedelta(days=120)

    recent, baseline = Counter(), Counter()
    versions = defaultdict(Counter)
    for r in rows:
        if r["type"] == "NON_ACTIONABLE" or not r["created"]:
            continue
        try:
            d = datetime.strptime(r["created"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d > recent_cut:
            recent[r["theme"]] += 1
            if r["app_version"]:
                versions[r["theme"]][r["app_version"]] += 1
        elif d > baseline_cut:
            baseline[r["theme"]] += 1

    out = []
    for theme, n in recent.most_common():
        if n < REGRESSION_MIN_COUNT:
            continue
        monthly_baseline = baseline[theme] / 3.0  # 90 days of baseline
        if monthly_baseline == 0:
            ratio, verdict = float("inf"), "new"
        else:
            ratio = n / monthly_baseline
            if ratio < REGRESSION_MULTIPLIER:
                continue
            verdict = "spiking"
        top_versions = versions[theme].most_common(3)
        concentrated = bool(top_versions) and top_versions[0][1] >= 0.5 * n
        out.append({
            "theme": theme,
            "recent_30d": n,
            "monthly_baseline": round(monthly_baseline, 1),
            "ratio": None if ratio == float("inf") else round(ratio, 1),
            "verdict": verdict,
            "app_versions": [{"version": v, "count": c} for v, c in top_versions],
            "release_correlated": concentrated,
        })
    return out


def build(rows, tax, start_date, end_date, as_of):
    actionable = [r for r in rows if r["type"] != "NON_ACTIONABLE"]
    theme_meta = {t["id"]: t for t in tax["themes"]}

    by_theme = defaultdict(lambda: {"total": 0, "sev": Counter(), "type": Counter(),
                                    "months": Counter(), "versions": Counter(),
                                    "platform": Counter()})
    for r in actionable:
        b = by_theme[r["theme"]]
        b["total"] += 1
        b["sev"][r["severity"]] += 1
        b["type"][r["type"]] += 1
        b["months"][_month(r["created"])] += 1
        b["platform"][r["platform"]] += 1
        if r["app_version"]:
            b["versions"][r["app_version"]] += 1

    months = sorted({_month(r["created"]) for r in rows if r["created"]})

    themes = []
    for tid, b in by_theme.items():
        meta = theme_meta.get(tid, {})
        impact = sum(IMPACT_WEIGHTS[s] * c for s, c in b["sev"].items())
        recent = b["months"].get(months[-1], 0) if months else 0
        prev = b["months"].get(months[-2], 0) if len(months) > 1 else 0
        themes.append({
            "id": tid,
            "label": meta.get("label", tid.replace("-", " ").title()),
            "area": meta.get("area", "Unclassified"),
            "why": meta.get("why", ""),
            # Some themes are blocking by definition, not by what the patient
            # wrote. Surface that so nobody reads the count as independent.
            "severity_floored": meta.get("severity_floor") == "BLOCKS_BOOKING",
            "total": b["total"],
            "impact": impact,
            "blocking": b["sev"]["BLOCKS_BOOKING"],
            "degrades": b["sev"]["DEGRADES"],
            "annoyance": b["sev"]["ANNOYANCE"],
            "defects": b["type"]["DEFECT"],
            "feature_requests": b["type"]["FEATURE_REQUEST"],
            "by_month": [b["months"].get(m, 0) for m in months],
            "latest_month": recent,
            "prev_month": prev,
            "trend": recent - prev,
            "top_versions": [{"version": v, "count": c} for v, c in b["versions"].most_common(3)],
        })
    themes.sort(key=lambda t: (-t["impact"], -t["total"]))

    by_area = defaultdict(lambda: {"total": 0, "blocking": 0, "themes": 0})
    for t in themes:
        a = by_area[t["area"]]
        a["total"] += t["total"]
        a["blocking"] += t["blocking"]
        a["themes"] += 1
    areas = sorted(
        ({"area": k, **v} for k, v in by_area.items()),
        key=lambda a: (-a["blocking"], -a["total"]),
    )

    noise = Counter(r["theme"] for r in rows if r["type"] == "NON_ACTIONABLE")
    diag = sum(1 for r in rows if r["has_diagnostics"])
    platforms = Counter(r["platform"] for r in rows if r["has_diagnostics"])

    volume = Counter(_month(r["created"]) for r in rows if r["created"])
    actionable_volume = Counter(_month(r["created"]) for r in actionable if r["created"])

    return {
        "generated_at": as_of.isoformat(),
        "window": {"start": start_date, "end": end_date},
        "totals": {
            "tickets": len(rows),
            "actionable": len(actionable),
            "noise": len(rows) - len(actionable),
            "noise_pct": round(100 * (len(rows) - len(actionable)) / len(rows)) if rows else 0,
            "blocking": sum(1 for r in actionable if r["severity"] == "BLOCKS_BOOKING"),
            "unclassified": sum(1 for r in actionable if r["theme"] == "unclassified"),
            "unclassified_pct": round(
                100 * sum(1 for r in actionable if r["theme"] == "unclassified") / len(actionable)
            ) if actionable else 0,
        },
        "months": months,
        "volume": [{"month": m, "total": volume[m], "actionable": actionable_volume[m]}
                   for m in months],
        "themes": themes,
        "areas": areas,
        "regressions": detect_regressions(rows, as_of),
        "level_shifts": detect_level_shifts(rows, as_of),
        "noise_breakdown": [{"theme": k, "count": v} for k, v in noise.most_common()],
        "diagnostics": {
            "with_app_info": diag,
            "coverage_pct": round(100 * diag / len(rows)) if rows else 0,
            "by_platform": [{"platform": k, "count": v} for k, v in platforms.most_common()],
        },
        "jira": {
            "all": _jql_link(
                f'project = AFH AND created >= "{start_date}" AND created <= "{end_date}" '
                f"ORDER BY created DESC"
            ),
            "open": _jql_link(
                f'project = AFH AND created >= "{start_date}" AND created <= "{end_date}" '
                f"AND statusCategory != Done ORDER BY created DESC"
            ),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classified", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--start-date", required=True)
    ap.add_argument("--end-date", required=True)
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD, defaults to end-date")
    args = ap.parse_args()

    with open(args.classified) as fh:
        rows = json.load(fh)

    as_of = datetime.strptime(args.as_of or args.end_date, "%Y-%m-%d").date()
    report = build(rows, load_taxonomy(), args.start_date, args.end_date, as_of)

    with open(args.output, "w") as fh:
        json.dump(report, fh, indent=2)

    t = report["totals"]
    print(f"report -> {args.output}")
    print(f"  {t['tickets']} tickets, {t['actionable']} actionable, "
          f"{t['blocking']} booking-blocking, {t['noise_pct']}% noise")
    for r in report["regressions"]:
        print(f"  REGRESSION [{r['verdict']}] {r['theme']}: {r['recent_30d']} in 30d "
              f"vs {r['monthly_baseline']}/mo baseline"
              + ("  (release-correlated)" if r["release_correlated"] else ""))
    for r in report["level_shifts"]:
        print(f"  LEVEL SHIFT {r['theme']}: {r['recent_90d']} in 90d "
              f"vs {r['prior_total']} in all prior history"
              + ("  (release-correlated)" if r["release_correlated"] else ""))


if __name__ == "__main__":
    main()
