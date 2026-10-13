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
from pathlib import Path
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


DATA_DIR = Path(__file__).resolve().parent.parent / "tests" / "data"


def _read_labels(name):
    f = DATA_DIR / name
    if not f.exists():
        return {}
    out = {}
    for line in f.read_text().splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("key\t"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            t = parts[1].strip()
            out[parts[0]] = "noise" if t.upper().startswith("NOISE") else t
    return out


def _accuracy(rows):
    """Score the classifier against the hand labels, at report time.

    Computed here rather than written down so the published accuracy figure can
    never drift from the classifier that produced the page. The test set was
    frozen before the last tuning round; the train set is what the patterns were
    tuned on, and the gap between the two is the point.
    """
    got = {r["key"]: (r["theme"] if r["type"] != "NON_ACTIONABLE" else "noise")
           for r in rows}

    def score(labels):
        pairs = [(g, got[k]) for k, g in labels.items() if k in got]
        hit = sum(1 for g, p in pairs if g == p)
        # Declining to classify a ticket a human called noise is a softer failure
        # than filing it under the wrong product area -- report it separately.
        len_hit = sum(1 for g, p in pairs
                      if g == p or (g == "noise" and p == "unclassified"))
        return len(pairs), hit, len_hit

    tn, th, _ = score(_read_labels("gold_set.tsv"))
    en, eh, el = score(_read_labels("test_set.tsv"))
    pct = lambda a, b: round(100 * a / b) if b else 0
    return {
        "train_n": tn, "train_correct": th, "train_pct": pct(th, tn),
        "heldout_n": en, "heldout_correct": eh, "heldout_pct": pct(eh, en),
        "heldout_lenient_correct": el, "heldout_lenient_pct": pct(el, en),
        "overfit_gap": pct(th, tn) - pct(eh, en),
    }


def _timeline(themes, months, rows, actionable):
    """One full record per month, not just a headline.

    The report is cumulative, so a single run already holds the whole monthly
    series -- there is no need to keep a history file to answer "what happened
    in March". Everything a month page shows is derived here so the templates
    stay dumb and this stays testable.

    Deltas compare against the immediately preceding month, so the first month
    reports no movers rather than a fake +N against zero.
    """
    series = {t["id"]: dict(zip(months, t["by_month"])) for t in themes}
    blocking = {t["id"]: dict(zip(months, t["blocking_by_month"])) for t in themes}
    meta = {t["id"]: t for t in themes}
    peaks = {t["id"]: max(series[t["id"]].values() or [0]) for t in themes}

    per_month = defaultdict(lambda: {"platform": Counter(), "versions": Counter(),
                                     "noise": Counter(), "sev": Counter(),
                                     "src": Counter()})
    for r in rows:
        if not r["created"]:
            continue
        m = per_month[_month(r["created"])]
        if r["type"] == "NON_ACTIONABLE":
            m["noise"][r["theme"]] += 1
    for r in actionable:
        if not r["created"]:
            continue
        m = per_month[_month(r["created"])]
        m["platform"][r["platform"]] += 1
        m["sev"][r["severity"]] += 1
        if r["severity"] == "BLOCKS_BOOKING":
            m["src"][r.get("severity_source", "text")] += 1
        if r["app_version"]:
            m["versions"][r["app_version"]] += 1

    out = []
    for i, month in enumerate(months):
        prev = months[i - 1] if i else None
        agg = per_month[month]

        active = []
        for tid, by_m in series.items():
            now = by_m.get(month, 0)
            was = by_m.get(prev, 0) if prev else 0
            if not now and not was:
                continue
            t = meta[tid]
            sub_label = {x["id"]: x["label"] for x in t["subthemes"]}
            sev = t["sev_by_month"].get(month, {})
            blk = sev.get("BLOCKS_BOOKING", blocking[tid].get(month, 0))
            deg = sev.get("DEGRADES", 0)
            ann = sev.get("ANNOYANCE", 0)
            active.append({
                "id": tid, "label": t["label"], "area": t["area"],
                "now": now, "was": was, "delta": now - was if prev else None,
                "blocking": blk, "degrades": deg, "annoyance": ann,
                # Same weighting the cumulative page ranks by, applied to one
                # month. Ranking a month by raw count would put a pile of minor
                # gripes above a handful of lost appointments.
                "impact": IMPACT_WEIGHTS["BLOCKS_BOOKING"] * blk
                + IMPACT_WEIGHTS["DEGRADES"] * deg
                + IMPACT_WEIGHTS["ANNOYANCE"] * ann,
                # The whole series travels with the row so a month page can draw
                # this month in the context of every other month.
                "series": [by_m.get(m, 0) for m in months],
                "series_max": peaks[tid],
                "index": i,
                # Whether this month is the busiest this theme has ever been --
                # a 3 that is a record reads differently from a 3 that is normal.
                "peak": now > 0 and now == peaks[tid],
                "subs": sorted(
                    ({"id": sid, "label": sub_label.get(sid, sid), "count": c[month]}
                     for sid, c in t["sub_by_month"].items() if c.get(month)),
                    key=lambda x: (-x["count"], x["label"]),
                ),
            })
        active.sort(key=lambda x: (-x["impact"], -x["now"], x["label"]))

        # "New" means never seen before this month, not merely absent last month.
        new = [a for a in active
               if a["now"] and not any(series[a["id"]].get(m) for m in months[:i])]
        quiet = [a for a in active if prev and a["was"] and not a["now"]]
        movers = [a for a in active if a["delta"]]
        movers.sort(key=lambda x: (-abs(x["delta"]), x["label"]))

        total = sum(a["now"] for a in active)
        shown = [a for a in active if a["now"]]
        impact = sum(a["impact"] for a in shown)

        areas = defaultdict(lambda: {"reports": 0, "blocking": 0, "impact": 0,
                                     "themes": 0})
        for a in shown:
            z = areas[a["area"]]
            z["reports"] += a["now"]
            z["blocking"] += a["blocking"]
            z["impact"] += a["impact"]
            z["themes"] += 1
        area_rows = sorted(
            ({"area": k, **v} for k, v in areas.items()),
            key=lambda x: (-x["impact"], -x["reports"], x["area"]),
        )

        # "Other" is the residue of a theme that had no specific pattern match,
        # so it names nothing. Useful inside one theme, meaningless in a list
        # ranking specific failures across all of them.
        subs = sorted((s for a in shown for s in
                       ({**x, "theme": a["label"], "theme_id": a["id"]}
                        for x in a["subs"] if x["id"] != "other")),
                      key=lambda x: (-x["count"], x["label"]))

        out.append({
            "month": month,
            "total": sum(1 for r in rows if r["created"] and _month(r["created"]) == month),
            "actionable": total,
            "blocking": agg["sev"]["BLOCKS_BOOKING"],
            # Of this month's blocking count, how much the patients actually
            # said versus how much a theme's severity floor asserted.
            "blocking_evidenced": agg["src"]["text"],
            "blocking_inferred": agg["src"]["theme_floor"],
            "blocking_inferred_pct": round(
                100 * agg["src"]["theme_floor"] / agg["sev"]["BLOCKS_BOOKING"]
            ) if agg["sev"]["BLOCKS_BOOKING"] else 0,
            "degrades": agg["sev"]["DEGRADES"],
            "annoyance": agg["sev"]["ANNOYANCE"],
            "noise": sum(agg["noise"].values()),
            "delta_actionable": total - sum(a["was"] for a in active) if prev else None,
            "themes_active": len(shown),
            "impact": impact,
            "rows": shown,
            "areas": area_rows,
            "subs": subs[:12],
            "worst": shown[0] if shown else None,
            "unclassified": sum(1 for r in actionable
                                if r["created"] and _month(r["created"]) == month
                                and r["theme"] == "unclassified"),
            "up": [x for x in movers if x["delta"] > 0][:3],
            "down": [x for x in movers if x["delta"] < 0][:3],
            "new": new,
            "quiet": quiet,
            "platform": [{"platform": k, "count": v} for k, v in agg["platform"].most_common()],
            "versions": [{"version": k, "count": v} for k, v in agg["versions"].most_common(6)],
            "diagnostics": sum(agg["versions"].values()),
            "noise_breakdown": [{"theme": k, "count": v} for k, v in agg["noise"].most_common()],
        })
    return out


def build(rows, tax, start_date, end_date, as_of):
    # The window is enforced here, not trusted from upstream. In CI fetch_data
    # already queries the window, but a wider input file (a cached fetch, a
    # backfill) would otherwise leak later tickets into an earlier month's
    # totals and fake a regression.
    rows = [r for r in rows if r["created"] and start_date <= r["created"][:10] <= end_date]
    actionable = [r for r in rows if r["type"] != "NON_ACTIONABLE"]
    theme_meta = {t["id"]: t for t in tax["themes"]}

    by_theme = defaultdict(lambda: {"total": 0, "sev": Counter(), "type": Counter(),
                                    "months": Counter(), "versions": Counter(),
                                    "platform": Counter(), "sub": defaultdict(Counter),
                                    "sub_months": defaultdict(Counter),
                                    "sev_months": defaultdict(Counter),
                                    "blocking_months": Counter(),
                                    "src": Counter()})
    for r in actionable:
        b = by_theme[r["theme"]]
        b["total"] += 1
        b["sev"][r["severity"]] += 1
        b["type"][r["type"]] += 1
        b["months"][_month(r["created"])] += 1
        b["sev_months"][_month(r["created"])][r["severity"]] += 1
        b["platform"][r["platform"]] += 1
        sid = r.get("subtheme") or "other"
        s = b["sub"][sid]
        s["total"] += 1
        s[r["severity"]] += 1
        b["sub_months"][sid][_month(r["created"])] += 1
        if r["severity"] == "BLOCKS_BOOKING":
            b["blocking_months"][_month(r["created"])] += 1
            b["src"][r.get("severity_source", "text")] += 1
        if r["app_version"]:
            b["versions"][r["app_version"]] += 1

    months = sorted({_month(r["created"]) for r in rows if r["created"]})
    total_blocking = sum(1 for r in actionable if r["severity"] == "BLOCKS_BOOKING")
    blocking_inferred = sum(1 for r in actionable if r["severity"] == "BLOCKS_BOOKING"
                            and r.get("severity_source") == "theme_floor")

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
                    "degrades": c["DEGRADES"],
                    "annoyance": c["ANNOYANCE"],
                    "pct": round(100 * c["total"] / b["total"]),
                    "by_month": [b["sub_months"][sid].get(m, 0) for m in months],
                    "first_month": next(
                        (m for m in months if b["sub_months"][sid].get(m)), None),
                    "last_month": next(
                        (m for m in reversed(months) if b["sub_months"][sid].get(m)), None),
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
            # Of the blocking count, how much the ticket text actually said
            # versus how much the theme floor asserted.
            "blocking_evidenced": b["src"]["text"],
            "blocking_inferred": b["src"]["theme_floor"],
            "degrades": b["sev"]["DEGRADES"],
            "annoyance": b["sev"]["ANNOYANCE"],
            "defects": b["type"]["DEFECT"],
            "feature_requests": b["type"]["FEATURE_REQUEST"],
            "dissatisfaction": b["type"]["DISSATISFACTION"],
            "by_month": [b["months"].get(m, 0) for m in months],
            "sev_by_month": {m: dict(b["sev_months"][m]) for m in months
                             if b["sev_months"][m]},
            "sub_by_month": {sid: dict(c) for sid, c in b["sub_months"].items()},
            "blocking_by_month": [b["blocking_months"].get(m, 0) for m in months],
            "latest_month": recent,
            "prev_month": prev,
            "trend": recent - prev,
            # One month is a noisy read on a theme that sees single digits.
            # The trailing quarter against the one before it is steadier.
            "q_recent": sum(b["months"].get(m, 0) for m in months[-3:]),
            "q_prior": sum(b["months"].get(m, 0) for m in months[-6:-3]),
            "qoq": (sum(b["months"].get(m, 0) for m in months[-3:])
                    - sum(b["months"].get(m, 0) for m in months[-6:-3])),
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
            "blocking": total_blocking,
            "blocking_inferred": blocking_inferred,
            "blocking_evidenced": total_blocking - blocking_inferred,
            "blocking_inferred_pct": round(100 * blocking_inferred / total_blocking)
                                     if total_blocking else 0,
            "unclassified": sum(1 for r in actionable if r["theme"] == "unclassified"),
            "unclassified_pct": round(
                100 * sum(1 for r in actionable if r["theme"] == "unclassified") / len(actionable)
            ) if actionable else 0,
        },
        "accuracy": _accuracy(rows),
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
        # Month-by-month trajectory, so the main page can show every month's
        # change without republishing or refetching anything.
        "timeline": _timeline(themes, months, rows, actionable),
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


def write_audit(rows, start_date, end_date, path):
    """Every published count, traceable back to the exact tickets behind it.

    This is the answer to "where does 42 come from": open the audit file, read
    the 42 keys, check them in Jira. It holds Jira keys (not bodies), which
    assert_no_phi.py bans from anything published -- the Pages site is public and
    a key there is a permanent pointer to a patient-authored complaint. So this
    file lives in the PHI workdir and CI shreds it with the rest.
    """
    rows = [
        r for r in rows if r["created"] and start_date <= r["created"][:10] <= end_date
    ]
    by_theme = {}
    for r in rows:
        if r["type"] == "NON_ACTIONABLE":
            bucket = f"NON_ACTIONABLE:{r.get('noise_rule') or 'unknown'}"
        else:
            bucket = r.get("theme") or "unclassified"
        entry = by_theme.setdefault(bucket, {"count": 0, "keys": [], "by_severity": {}})
        entry["count"] += 1
        entry["keys"].append(r["key"])
        sev = r.get("severity", "n/a")
        entry["by_severity"][sev] = entry["by_severity"].get(sev, 0) + 1

    for entry in by_theme.values():
        entry["keys"].sort(key=lambda k: (k.split("-")[0], int(k.split("-")[1])))
        # Paste into Jira to see exactly this bucket.
        entry["jql"] = f"key in ({', '.join(entry['keys'])})"

    out = {
        "window": {"start": start_date, "end": end_date},
        "warning": "Contains Jira ticket keys. Local audit only — never publish.",
        "themes": dict(sorted(by_theme.items(), key=lambda kv: -kv[1]["count"])),
    }
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classified", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--start-date", required=True)
    ap.add_argument("--end-date", required=True)
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD, defaults to end-date")
    ap.add_argument(
        "--audit-output",
        default=None,
        help="Optional theme->ticket-key map for auditing a number back to source. "
        "Contains Jira keys, so it stays in the PHI workdir and is never published.",
    )
    args = ap.parse_args()

    with open(args.classified) as fh:
        rows = json.load(fh)

    as_of = datetime.strptime(args.as_of or args.end_date, "%Y-%m-%d").date()
    report = build(rows, load_taxonomy(), args.start_date, args.end_date, as_of)

    with open(args.output, "w") as fh:
        json.dump(report, fh, indent=2)

    t = report["totals"]
    if args.audit_output:
        write_audit(rows, args.start_date, args.end_date, args.audit_output)
        print(f"audit  -> {args.audit_output}  (ticket keys — do not publish)")

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
