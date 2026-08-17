import os
import subprocess
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from compute_report import build, detect_regressions  # noqa: E402
from config import load_taxonomy  # noqa: E402

TAX = load_taxonomy()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def row(theme="insurance-network-accuracy", created="2026-08-01",
        severity="BLOCKS_BOOKING", type_="DEFECT", version="4.230.0"):
    return {
        "key": "AFH-1", "created": created, "app_version": version,
        "has_diagnostics": bool(version), "platform": "iOS", "status": "Done",
        "area": "Insurance", "theme": theme, "theme_label": "x",
        "type": type_, "severity": severity,
    }


def test_impact_ranking_beats_raw_volume():
    rows = [row(theme="loud", severity="ANNOYANCE") for _ in range(20)]
    rows += [row(theme="harmful", severity="BLOCKS_BOOKING") for _ in range(8)]
    report = build(rows, TAX, "2026-08-01", "2026-08-31", date(2026, 8, 31))
    assert report["themes"][0]["id"] == "harmful"


def test_new_theme_flagged_as_new_not_spiking():
    rows = [row(theme="brand-new", created="2026-08-10") for _ in range(6)]
    regs = detect_regressions(rows, date(2026, 8, 20))
    assert regs and regs[0]["verdict"] == "new"
    assert regs[0]["ratio"] is None


def test_single_version_spike_is_release_correlated():
    rows = [row(theme="t", created="2026-05-01") for _ in range(9)]
    rows += [row(theme="t", created="2026-08-10", version="4.230.0") for _ in range(6)]
    regs = detect_regressions(rows, date(2026, 8, 20))
    assert regs[0]["release_correlated"] is True


def test_spread_across_versions_is_not_release_correlated():
    rows = [row(theme="t", created="2026-05-01") for _ in range(9)]
    rows += [row(theme="t", created="2026-08-10", version=f"4.2{n}.0") for n in range(20, 26)]
    regs = detect_regressions(rows, date(2026, 8, 20))
    assert regs[0]["release_correlated"] is False


def test_non_actionable_excluded_from_regression_detection():
    rows = [row(theme="noise", type_="NON_ACTIONABLE", created="2026-08-10") for _ in range(30)]
    assert detect_regressions(rows, date(2026, 8, 20)) == []


def test_phi_guard_rejects_a_report_containing_a_ticket_key(tmp_path):
    bad = tmp_path / "report.json"
    bad.write_text('{"themes": [{"label": "see AFH-412 for detail"}]}')
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "assert_no_phi.py"), "--report", str(bad)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "jira ticket key" in proc.stderr


def test_phi_guard_allows_a_clean_aggregate_report(tmp_path):
    good = tmp_path / "report.json"
    good.write_text('{"themes": [{"label": "In-network status is wrong", "total": 36}]}')
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "assert_no_phi.py"), "--report", str(good)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
