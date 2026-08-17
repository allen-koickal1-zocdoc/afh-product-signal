from datetime import date

import pytest

from scripts.resolve_window import resolve

FIRST = "2025-12-01"


def test_default_is_the_last_complete_month():
    w = resolve("", FIRST, date(2026, 9, 2))
    assert w["month"] == "2026-08"
    assert w["end"] == "2026-08-31"
    assert w["label"] == "August 2026"
    assert w["partial"] == "false"


def test_january_run_rolls_back_to_december():
    w = resolve("", FIRST, date(2026, 1, 2))
    assert w["month"] == "2025-12"
    assert w["end"] == "2025-12-31"


def test_history_always_starts_at_the_first_ticket():
    """The window selects the end, never the start -- trends need full history."""
    for today in (date(2026, 3, 2), date(2026, 9, 2)):
        assert resolve("", FIRST, today)["start"] == FIRST


def test_current_month_clamps_to_today_and_says_so():
    w = resolve("2026-08", FIRST, date(2026, 8, 17))
    assert w["end"] == "2026-08-17"
    assert w["partial"] == "true"
    assert "month to date" in w["label"]


def test_leap_february_ends_on_the_29th():
    w = resolve("2028-02", FIRST, date(2028, 5, 1))
    assert w["end"] == "2028-02-29"


def test_future_month_is_rejected():
    with pytest.raises(SystemExit):
        resolve("2027-01", FIRST, date(2026, 8, 17))


@pytest.mark.parametrize("bad", ["2026-13", "August", "2026", "2026-1"])
def test_malformed_month_is_rejected(bad):
    with pytest.raises(SystemExit):
        resolve(bad, FIRST, date(2026, 8, 17))
