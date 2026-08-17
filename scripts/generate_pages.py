#!/usr/bin/env python3
"""generate_pages.py -- render report.json into the static dashboard."""

import argparse
import json
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)


def render(report: dict, output_dir: str):
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )

    labels = {t["id"]: t["label"] for t in report["themes"]}
    env.globals["theme_label"] = lambda tid: labels.get(tid, tid.replace("-", " ").title())

    themes = report["themes"]
    top = themes[0] if themes else None
    blocking_total = report["totals"]["blocking"]
    top3_blocking = sum(t["blocking"] for t in themes[:3])
    top3_pct = round(100 * top3_blocking / blocking_total) if blocking_total else 0
    max_month = max((max(t["by_month"]) for t in themes if t["by_month"]), default=0)

    html = env.get_template("index.html").render(
        report=report, top=top, top3_pct=top3_pct, max_month=max_month
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html)
    (out / "report.json").write_text(json.dumps(report, indent=2))
    return out / "index.html"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args()

    with open(args.report) as fh:
        report = json.load(fh)

    path = render(report, args.output_dir)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
