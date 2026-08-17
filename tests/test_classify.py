import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from classify import classify_one  # noqa: E402
from config import load_taxonomy  # noqa: E402

TAX = load_taxonomy()


def ticket(summary="Feedback for Zocdoc iOS App", body="", **kw):
    base = {
        "key": "AFH-999", "summary": summary, "body": body, "status": "Done",
        "labels": [], "created": "2026-08-01", "app_version": "4.230.0",
        "os": "iOS 18.2", "device": "iPhone 15", "has_diagnostics": True,
        "is_synthetic": False,
    }
    base.update(kw)
    return base


def test_survey_blast_is_non_actionable():
    row = classify_one(ticket(body="We'd love your feedback on your recent visit."), TAX)
    assert row["type"] == "NON_ACTIONABLE"
    assert row["theme"] == "csat-survey-blast"


def test_generic_subject_with_no_body_is_empty():
    row = classify_one(ticket(), TAX)
    assert row["theme"] == "empty-submission"
    assert row["severity"] == "NONE"


def test_synthetic_tickets_never_count_as_signal():
    row = classify_one(ticket(body="Search is completely broken", is_synthetic=True), TAX)
    assert row["type"] == "NON_ACTIONABLE"
    assert row["theme"] == "internal-test"


def test_network_accuracy_is_blocking_even_without_harm_words():
    row = classify_one(ticket(body="The listing said in-network but they were out of network."), TAX)
    assert row["theme"] == "insurance-network-accuracy"
    assert row["severity"] == "BLOCKS_BOOKING"


def test_financial_harm_escalates_severity():
    row = classify_one(ticket(body="Filter showed the wrong hours and I got an unexpected bill."), TAX)
    assert row["severity"] == "BLOCKS_BOOKING"


def test_feature_request_is_not_a_defect():
    row = classify_one(ticket(body="It would be great if you could add a dark mode toggle."), TAX)
    assert row["type"] == "FEATURE_REQUEST"


def test_soft_noise_never_pre_empts_a_real_theme():
    """Praise wrapped around a defect is a defect. The fallback runs last for exactly this."""
    row = classify_one(
        ticket(body="Great app and I highly recommend it, but the provider was out of network."),
        TAX,
    )
    assert row["theme"] == "insurance-network-accuracy"
    assert row["type"] != "NON_ACTIONABLE"


def test_praise_with_no_defect_is_still_dropped():
    row = classify_one(ticket(body="Excellent service, I would definitely recommend. Thank you"), TAX)
    assert row["type"] == "NON_ACTIONABLE"


def test_unmatched_text_lands_in_unclassified_not_a_random_theme():
    row = classify_one(ticket(body="Kwyjibo florble the grimble sprocket vexation quandary."), TAX)
    assert row["theme"] == "unclassified"
    assert row["area"] == "Unclassified"


def test_no_free_text_leaks_into_the_row():
    secret = "my name is redacted and my claim was denied"
    row = classify_one(ticket(body=secret), TAX)
    assert secret not in " ".join(str(v) for v in row.values())
