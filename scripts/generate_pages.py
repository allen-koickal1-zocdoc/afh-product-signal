#!/usr/bin/env python3
"""
generate_pages.py -- render report.json into the static dashboard.

Emits:
  index.html            the ranked overview
  themes/<id>.html      one page per theme: sub-themes, versions, platform, trend
  report.json           the full aggregate, for anything that wants to read it
  themes.csv            one row per theme
  subthemes.csv         one row per sub-theme
  monthly.csv           theme x month matrix, long format

The CSVs exist because the first thing anyone does with a dashboard is ask for
the numbers in a spreadsheet. Better to publish them than to have people
re-derive them by hand and get it wrong.
"""

import argparse
import csv
import json
import re
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)


def _env(report: dict) -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    labels = {t["id"]: t["label"] for t in report["themes"]}
    env.globals["theme_label"] = lambda tid: labels.get(tid, tid.replace("-", " ").title())
    env.globals["has_page"] = lambda tid: tid in labels
    # A theme's monthly count means little without the month it sits in: 4 of 12
    # and 4 of 60 are different stories.
    totals = {m["month"]: m["actionable"] for m in report["timeline"]}
    env.globals["month_total"] = lambda month: totals.get(month, 0)
    return env


def write_csvs(report: dict, out: Path):
    with (out / "themes.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["theme", "label", "area", "reports", "blocking", "degrades",
                    "annoyance", "defects", "feature_requests", "impact",
                    "share_of_blocking_pct", "first_month", "last_month",
                    "months_active", "prev_month_count", "latest_month_count",
                    "severity_floored"])
        for t in report["themes"]:
            w.writerow([t["id"], t["label"], t["area"], t["total"], t["blocking"],
                        t["degrades"], t["annoyance"], t["defects"],
                        t["feature_requests"], t["impact"], t["share_of_blocking"],
                        t["first_month"], t["last_month"], t["months_active"],
                        t["prev_month"], t["latest_month"], t["severity_floored"]])

    with (out / "subthemes.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["theme", "theme_label", "area", "subtheme", "subtheme_label",
                    "reports", "blocking", "pct_of_theme"])
        for t in report["themes"]:
            # A theme with no configured sub-themes yields one "other, 100%"
            # row that says nothing. Skip it rather than pad the file.
            if t["subtheme_coverage_pct"] is None:
                continue
            for s in t["subthemes"]:
                w.writerow([t["id"], t["label"], t["area"], s["id"], s["label"],
                            s["total"], s["blocking"], s["pct"]])

    with (out / "monthly.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["month", "theme", "label", "area", "reports", "blocking"])
        for t in report["themes"]:
            for i, m in enumerate(report["months"]):
                if t["by_month"][i]:
                    w.writerow([m, t["id"], t["label"], t["area"],
                                t["by_month"][i], t["blocking_by_month"][i]])


def render_index(report: dict, out: Path, archived_months):
    """Write index.html alone.

    Split out because the month-by-month table can only link snapshots that
    actually exist on the published branch, and that list is not known until
    publish time -- so publish.py re-renders this one page after cloning.
    """
    themes = report["themes"]
    blocking_total = report["totals"]["blocking"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(_env(report).get_template("index.html").render(
        report=report,
        top=themes[0] if themes else None,
        top3_pct=(round(100 * sum(t["blocking"] for t in themes[:3]) / blocking_total)
                  if blocking_total else 0),
        max_month=max((max(t["by_month"]) for t in themes if t["by_month"]), default=0),
        archived=set(archived_months or []),
        depth="",
    ))


def render(report: dict, output_dir: str, archived_months=None):
    env = _env(report)

    themes = report["themes"]
    top = themes[0] if themes else None
    blocking_total = report["totals"]["blocking"]
    top3_pct = (round(100 * sum(t["blocking"] for t in themes[:3]) / blocking_total)
                if blocking_total else 0)
    max_month = max((max(t["by_month"]) for t in themes if t["by_month"]), default=0)

    out = Path(output_dir)
    (out / "themes").mkdir(parents=True, exist_ok=True)

    # Which months already have a permalink. Publishing overlays onto the live
    # gh-pages tree, so the archive that exists there is the source of truth --
    # the timeline links only months that will actually resolve.
    archived = set(archived_months or [])
    archived.add(report["months"][-1] if report["months"] else None)

    render_index(report, out, archived)

    tpl = env.get_template("theme.html")
    for t in themes:
        peak = max(t["by_month"]) or 1
        (out / "themes" / f"{t['id']}.html").write_text(tpl.render(
            report=report, theme=t, peak=peak, depth="../"
        ))

    # One page per month. These live in the main tree rather than inside the
    # frozen archive snapshots, so every month in the window is reachable even
    # if the report was never published separately that month.
    (out / "months").mkdir(exist_ok=True)
    tpl = env.get_template("month.html")
    jql = {v["month"]: v["jql"] for v in report["volume"]}
    timeline = report["timeline"]
    for i, m in enumerate(timeline):
        (out / "months" / f"{m['month']}.html").write_text(tpl.render(
            report=report, m=m,
            prev=timeline[i - 1]["month"] if i else None,
            next=timeline[i + 1]["month"] if i + 1 < len(timeline) else None,
            partial=report["partial_last_month"] and i == len(timeline) - 1,
            month_jql=jql.get(m["month"], report["jira"]["all"]),
            depth="../",
        ))

    (out / "report.json").write_text(json.dumps(report, indent=2))
    write_csvs(report, out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument(
        "--archive-dir",
        default=None,
        help="Existing published archive/ (from the live site). Months found here "
        "get linked from the month-by-month table; missing ones are marked unpublished.",
    )
    args = ap.parse_args()

    with open(args.report) as fh:
        report = json.load(fh)

    archived = []
    if args.archive_dir and Path(args.archive_dir).is_dir():
        archived = [d.name for d in Path(args.archive_dir).iterdir()
                    if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}", d.name)]

    out = render(report, args.output_dir, archived_months=archived)
    n = len(report["themes"])
    print(f"wrote {out}/index.html + {n} theme pages "
          f"+ {len(report['timeline'])} month pages + 3 CSVs")


if __name__ == "__main__":
    main()
