"""Tests for the drill-down layer: sub-themes, movers, exports, and the guard."""

import csv
import json
import os
import subprocess
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from classify import classify_one  # noqa: E402
from compute_report import build  # noqa: E402
from config import load_taxonomy  # noqa: E402
from generate_pages import render  # noqa: E402

TAX = load_taxonomy()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ticket(body, summary="Feedback", created="2026-08-01"):
    return {"key": "AFH-1", "summary": summary, "body": body, "labels": [],
            "created": created, "app_version": "4.230.0", "has_diagnostics": True,
            "os": "iOS 18", "status": "Done"}


def row(theme="insurance-network-accuracy", created="2026-08-01",
        severity="BLOCKS_BOOKING", subtheme="other"):
    return {"key": "AFH-1", "created": created, "app_version": "4.230.0",
            "has_diagnostics": True, "platform": "iOS", "status": "Done",
            "area": "Insurance", "theme": theme, "theme_label": "x",
            "subtheme": subtheme, "subtheme_label": "x",
            "type": "DEFECT", "severity": severity}


# --- sub-theme classification -------------------------------------------------

def test_subtheme_is_assigned_within_a_theme():
    r = classify_one(ticket("They said in-network but I was billed out of network"), TAX)
    assert r["theme"] == "insurance-network-accuracy"
    assert r["subtheme"] != "other"


def test_unmatched_subtheme_falls_to_other_not_to_the_nearest_guess():
    """Other is a real answer. Silently picking the closest sub-theme would lie."""
    r = classify_one(ticket("This provider was listed as in-network and is not."), TAX)
    assert r["theme"] == "insurance-network-accuracy"
    assert r["subtheme"] in {s["id"] for s in
                             next(t for t in TAX["themes"]
                                  if t["id"] == "insurance-network-accuracy")["subthemes"]} \
        or r["subtheme"] == "other"


def test_noise_rows_carry_no_subtheme():
    r = classify_one(ticket("(no text)", summary="Feedback for Zocdoc iOS App"), TAX)
    assert r["type"] == "NON_ACTIONABLE"
    assert r["subtheme"] == ""


def test_coverage_is_none_when_a_theme_has_no_subthemes():
    """None means 'not broken out', which is different from 0% coverage."""
    report = build([row(theme="messaging")], TAX, "2026-08-01", "2026-08-31",
                   date(2026, 8, 31))
    assert report["themes"][0]["subtheme_coverage_pct"] is None


def test_coverage_excludes_the_other_bucket():
    rows = [row(subtheme="billed-after-booking") for _ in range(3)]
    rows += [row(subtheme="other") for _ in range(1)]
    report = build(rows, TAX, "2026-08-01", "2026-08-31", date(2026, 8, 31))
    assert report["themes"][0]["subtheme_coverage_pct"] == 75


def test_other_sorts_last_even_when_it_is_the_biggest_bucket():
    rows = [row(subtheme="other") for _ in range(9)]
    rows += [row(subtheme="billed-after-booking")]
    report = build(rows, TAX, "2026-08-01", "2026-08-31", date(2026, 8, 31))
    assert report["themes"][0]["subthemes"][-1]["id"] == "other"


# --- movers -------------------------------------------------------------------

def test_movers_rank_by_ratio_not_raw_delta():
    """1 -> 6 is news; 20 -> 26 is the same delta and is not."""
    rows = [row(theme="small", created="2026-07-05")]
    rows += [row(theme="small", created="2026-08-05") for _ in range(6)]
    rows += [row(theme="big", created="2026-07-05") for _ in range(20)]
    rows += [row(theme="big", created="2026-08-05") for _ in range(26)]
    report = build(rows, TAX, "2026-07-01", "2026-08-31", date(2026, 8, 31))
    assert report["movers"]["up"][0]["id"] == "small"


def test_small_swings_are_ignored():
    rows = [row(theme="t", created="2026-07-05") for _ in range(4)]
    rows += [row(theme="t", created="2026-08-05") for _ in range(6)]
    report = build(rows, TAX, "2026-07-01", "2026-08-31", date(2026, 8, 31))
    assert report["movers"]["up"] == []


def test_partial_final_month_suppresses_movers_entirely():
    """Half a month makes every theme look like it collapsed. Show nothing."""
    rows = [row(theme="t", created="2026-07-05") for _ in range(10)]
    rows += [row(theme="t", created="2026-08-05")]
    report = build(rows, TAX, "2026-07-01", "2026-08-17", date(2026, 8, 17))
    assert report["partial_last_month"] is True
    assert report["movers"]["suppressed"] is True
    assert report["movers"]["down"] == []


def test_complete_final_month_does_not_suppress():
    report = build([row()], TAX, "2026-08-01", "2026-08-31", date(2026, 8, 31))
    assert report["partial_last_month"] is False
    assert report["movers"]["suppressed"] is False


def test_february_month_end_is_not_treated_as_partial():
    report = build([row(created="2026-02-10")], TAX, "2026-02-01", "2026-02-28",
                   date(2026, 2, 28))
    assert report["partial_last_month"] is False


# --- exports ------------------------------------------------------------------

def test_csv_exports_carry_every_theme_and_no_ticket_keys(tmp_path):
    rows = [row(subtheme="billed-after-booking"), row(theme="messaging")]
    report = build(rows, TAX, "2026-08-01", "2026-08-31", date(2026, 8, 31))
    render(report, str(tmp_path))

    themes = list(csv.DictReader((tmp_path / "themes.csv").open()))
    assert len(themes) == len(report["themes"])

    # messaging has no sub-themes, so it must not contribute a bogus 100% row
    subs = list(csv.DictReader((tmp_path / "subthemes.csv").open()))
    assert {s["theme"] for s in subs} == {"insurance-network-accuracy"}

    blob = "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    assert "AFH-1" not in blob


def test_every_theme_gets_a_page(tmp_path):
    report = build([row(), row(theme="messaging")], TAX, "2026-08-01", "2026-08-31",
                   date(2026, 8, 31))
    render(report, str(tmp_path))
    for t in report["themes"]:
        assert (tmp_path / "themes" / f"{t['id']}.html").exists()


# --- the guard ----------------------------------------------------------------

def _guard(*args):
    return subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "assert_no_phi.py"), *args],
        capture_output=True, text=True)


def test_guard_scans_html_not_just_json(tmp_path):
    """report.json can be clean while a template leaks. Scan what is served."""
    (tmp_path / "index.html").write_text("<p>see AFH-402 for detail</p>")
    assert _guard("--pages-dir", str(tmp_path)).returncode == 1


def test_guard_scans_csv_exports(tmp_path):
    (tmp_path / "themes.csv").write_text("theme,note\nx,patient@example.com\n")
    assert _guard("--pages-dir", str(tmp_path)).returncode == 1


def test_guard_passes_on_real_generated_output(tmp_path):
    report = build([row()], TAX, "2026-08-01", "2026-08-31", date(2026, 8, 31))
    render(report, str(tmp_path))
    (tmp_path / "r.json").write_text(json.dumps(report))
    assert _guard("--report", str(tmp_path / "r.json"),
                  "--pages-dir", str(tmp_path)).returncode == 0


def test_guard_requires_something_to_check():
    assert _guard().returncode != 0
