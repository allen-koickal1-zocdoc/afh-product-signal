#!/usr/bin/env python3
"""
compute_report.py -- aggregate classified rows into the published report.

This is the PHI boundary. Input rows are patient-level (one AFH ticket is one
patient). Everything written to report.json is a count, a rate or a theme label.
No ticket keys, no dates-per-ticket, no free text.
"""

import argparse
import calendar
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


def _next_month(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    return f"{y + 1:04d}-01" if mo == 12 else f"{y:04d}-{mo + 1:02d}"


def _last_month_partial(end_date: str) -> bool:
    """
    True when the window stops mid-month.

    Month-over-month comparisons are worthless against a partial month -- every
    theme reads as collapsing, because half the month has not happened yet. The
    scheduled run ends on a month boundary and avoids this; a month-to-date run
    does not, so the page has to say so rather than show a table of fake drops.
    """
    d = datetime.strptime(end_date, "%Y-%m-%d").date()
    return d.day != calendar.monthrange(d.year, d.month)[1]


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


def _movers(themes, months, limit=5):
    """
    Biggest month-over-month swings, as a share of the theme's own baseline.

    Raw deltas just re-rank the big themes -- a theme going 20 -> 26 outranks
    one going 1 -> 6, though only the second is news. Ranking by ratio with a
    floor on the absolute change keeps small-number noise out.
    """
    if len(months) < 2:
        return {"up": [], "down": []}
    out = []
    for t in themes:
        now, before = t["latest_month"], t["prev_month"]
        delta = now - before
        if abs(delta) < 3:
            continue
        out.append({
            "id": t["id"], "label": t["label"], "area": t["area"],
            "prev": before, "now": now, "delta": delta,
            "ratio": round(now / before, 1) if before else None,
        })
    up = sorted([m for m in out if m["delta"] > 0],
                key=lambda m: (-(m["ratio"] or 99), -m["delta"]))[:limit]
    down = sorted([m for m in out if m["delta"] < 0], key=lambda m: m["delta"])[:limit]
    return {"up": up, "down": down}


def build(rows, tax, start_date, end_date, as_of):
    actionable = [r for r in rows if r["type"] != "NON_ACTIONABLE"]
    theme_meta = {t["id"]: t for t in tax["themes"]}

    by_theme = defaultdict(lambda: {"total": 0, "sev": Counter(), "type": Counter(),
                                    "months": Counter(), "versions": Counter(),
                                    "platform": Counter(), "sub": defaultdict(Counter),
                                    "blocking_months": Counter()})
    for r in actionable:
        b = by_theme[r["theme"]]
        b["total"] += 1
        b["sev"][r["severity"]] += 1
        b["type"][r["type"]] += 1
        b["months"][_month(r["created"])] += 1
        b["platform"][r["platform"]] += 1
        s = b["sub"][r.get("subtheme") or "other"]
        s["total"] += 1
        s[r["severity"]] += 1
        if r["severity"] == "BLOCKS_BOOKING":
            b["blocking_months"][_month(r["created"])] += 1
        if r["app_version"]:
            b["versions"][r["app_version"]] += 1

    months = sorted({_month(r["created"]) for r in rows if r["created"]})
    total_blocking = sum(1 for r in actionable if r["severity"] == "BLOCKS_BOOKING")

    themes = []
    for tid, b in by_theme.items():
        meta = theme_meta.get(tid, {})
        impact = sum(IMPACT_WEIGHTS[s] * c for s, c in b["sev"].items())
        recent = b["months"].get(months[-1], 0) if months else 0
        prev = b["months"].get(months[-2], 0) if len(months) > 1 else 0
        active = [m for m in months if b["months"].get(m)]

        sub_labels = {s["id"]: s["label"] for s in meta.get("subthemes", [])}
        sub_labels["other"] = "Other / not broken out"
        subthemes = sorted(
            (
                {
                    "id": sid,
                    "label": sub_labels.get(sid, sid.replace("-", " ").capitalize()),
                    "total": c["total"],
                    "blocking": c["BLOCKS_BOOKING"],
                    "pct": round(100 * c["total"] / b["total"]),
                }
                for sid, c in b["sub"].items()
            ),
            key=lambda s: (s["id"] == "other", -s["total"]),  # Other always last
        )

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
            "dissatisfaction": b["type"]["DISSATISFACTION"],
            "by_month": [b["months"].get(m, 0) for m in months],
            "blocking_by_month": [b["blocking_months"].get(m, 0) for m in months],
            "latest_month": recent,
            "prev_month": prev,
            "trend": recent - prev,
            "first_month": active[0] if active else None,
            "last_month": active[-1] if active else None,
            "months_active": len(active),
            "share_of_blocking": round(100 * b["sev"]["BLOCKS_BOOKING"] / total_blocking)
                                 if total_blocking else 0,
            "platform": [{"platform": p, "count": c} for p, c in b["platform"].most_common()],
            "subthemes": subthemes,
            # How much of the theme the sub-themes actually explain. Low
            # coverage means the breakdown below is not worth trusting yet.
            "subtheme_coverage_pct": round(
                100 * sum(s["total"] for s in subthemes if s["id"] != "other") / b["total"]
            ) if meta.get("subthemes") else None,
            "top_versions": [{"version": v, "count": c} for v, c in b["versions"].most_common(3)],
            "all_versions": [{"version": v, "count": c} for v, c in b["versions"].most_common()],
        })
    themes.sort(key=lambda t: (-t["impact"], -t["total"]))

    by_area = defaultdict(lambda: {"total": 0, "blocking": 0, "impact": 0,
                                   "feature_requests": 0, "theme_list": []})
    for t in themes:
        a = by_area[t["area"]]
        a["total"] += t["total"]
        a["blocking"] += t["blocking"]
        a["impact"] += t["impact"]
        a["feature_requests"] += t["feature_requests"]
        a["theme_list"].append({"id": t["id"], "label": t["label"],
                                "total": t["total"], "blocking": t["blocking"]})
    areas = sorted(
        ({"area": k, "themes": len(v["theme_list"]), **v} for k, v in by_area.items()),
        key=lambda a: (-a["blocking"], -a["total"]),
    )

    partial = _last_month_partial(end_date)
    movers = {"up": [], "down": [], "suppressed": True} if partial \
        else {**_movers(themes, months), "suppressed": False}
    # A theme first seen in the month being reported has no history to compare
    # against, so it is worth calling out separately from a spike.
    first_seen = [
        {"id": t["id"], "label": t["label"], "area": t["area"], "total": t["total"],
         "blocking": t["blocking"]}
        for t in themes if months and t["first_month"] == months[-1]
    ]

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
        "partial_last_month": partial,
        # Each month links to its own JQL. The dashboard cannot show ticket
        # keys, so this is the only sanctioned route from a number to the
        # tickets behind it -- and Jira is permission-gated, which the page is not.
        "volume": [{"month": m, "total": volume[m], "actionable": actionable_volume[m],
                    "jql": _jql_link(
                        f'project = AFH AND created >= "{m}-01" '
                        f'AND created < "{_next_month(m)}-01" ORDER BY created DESC')}
                   for m in months],
        "themes": themes,
        "areas": areas,
        "movers": movers,
        "first_seen": first_seen,
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
