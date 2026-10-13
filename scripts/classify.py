#!/usr/bin/env python3
"""
classify.py -- turn raw AFH tickets into product-signal rows.

Deterministic rules only (config/taxonomy.json). No model calls, so a given
ticket always lands in the same theme and the numbers on the dashboard are
reproducible from the config.

Input:  tickets.json  (contains PHI)
Output: classified.json (no free text -- key, date, theme, type, severity, version)

Anything the rules cannot place lands in the `unclassified` bucket, which the
dashboard reports as a coverage number. A rising unclassified rate means the
taxonomy is stale, not that the queue is quiet.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from config import load_taxonomy  # noqa: E402

# BLOCKS_BOOKING means the patient lost the appointment or lost money. Keep this
# aligned with how patients actually phrase abandonment -- most never say
# "I could not book", they say "I gave up" or "I called the office instead".
BLOCKING = re.compile(
    # abandoned the funnel
    r"could ?n'?t (book|complete|schedule|get an appointment)|cannot book|unable to (book|schedule|complete)|"
    r"gave up|gave up on|deleted the app|uninstall|stopped using|went (elsewhere|somewhere else|to another)|"
    r"had to (call|phone|go elsewhere|find another|book (directly|by phone)|use (the )?(website|desktop))|"
    r"ended up (calling|booking directly|going)|useless|can'?t use the app|"
    # lost the appointment
    r"lost (the|my) appointment|missed (the|my) appointment|turned away|sent home|"
    r"(appointment|booking) (was )?(cancell?ed|no ?showed|not honou?red)|wasted (a |the |my )?(trip|visit|day)|"
    r"drove (all the way|there) (and|only)|showed up (and|only to)|"
    # financial harm
    r"unexpected bill|surprise bill|(got |was |been )charged|denied (the |my )?claim|claim was denied|"
    r"out.?of.?pocket|balance.?bill|\$\d|paid (out of pocket|the full)|"
    # cannot get in at all
    r"locked out|can'?t (log ?in|sign ?in|book|search|get (in|past))|"
    r"(no|zero|0) (providers?|results?|doctors?|availability|appointments?)( (at all|found|show|showed|come up|returned))?|"
    r"nothing (comes? up|shows? up|is returned)|returns? nothing|empty results",
    re.I,
)
DEGRADING = re.compile(
    r"wrong|incorrect|inaccurate|doesn'?t work|not working|broken|failed|error|"
    r"had to (try|retry)|takes (too )?long|confusing|misleading",
    re.I,
)
FEATURE = re.compile(
    r"\b(please add|would be (nice|great|helpful)|wish (you|it)|feature request|"
    r"suggestion|it would help if|why (can'?t|don'?t) you|allow (me|us) to|there should be)\b",
    re.I,
)
DISSATISFIED = re.compile(
    r"\b(hate|dislike|worst|terrible|awful|annoying|frustrat|prefer the old|don'?t like)\b",
    re.I,
)


# The subject line the app puts on every submission. Carries no signal on its own,
# so a ticket whose only content is this subject is an empty submission.
GENERIC_SUBJECT = re.compile(r"^\s*feedback for (the )?zocdoc\s*(ios|android)?\s*app\s*$", re.I)


def _hay(ticket: dict) -> str:
    return " \n ".join(
        [ticket.get("summary", ""), ticket.get("body", ""), " ".join(ticket.get("labels", []))]
    )


def _is_empty(ticket: dict) -> bool:
    body = (ticket.get("body") or "").strip()
    if len(body) >= 15:
        return False
    summary = (ticket.get("summary") or "").strip()
    return bool(GENERIC_SUBJECT.match(summary)) or len(summary) < 8


def classify_one(ticket: dict, tax: dict) -> dict:
    key = ticket.get("key", "")
    hay = _hay(ticket)
    row = {
        "key": key,
        "created": ticket.get("created", ""),
        "app_version": ticket.get("app_version", ""),
        "has_diagnostics": bool(ticket.get("has_diagnostics")),
        "platform": _platform(ticket),
        "status": ticket.get("status", ""),
        # Overwritten below when a theme with sub-themes matches. Present on
        # every row so downstream code never has to guard for the key.
        "subtheme": "",
        "subtheme_label": "",
    }

    if ticket.get("is_synthetic"):
        return {**row, "area": "Unclassified", "theme": "internal-test",
                "theme_label": "Internal test ticket", "type": "NON_ACTIONABLE", "severity": "NONE", "severity_source": "n/a"}

    def _noise(rule):
        return {**row, "area": "Unclassified", "theme": rule["id"],
                "theme_label": rule["label"], "type": "NON_ACTIONABLE", "severity": "NONE", "severity_source": "n/a"}

    # Hard noise: intake that can never carry product signal. Short-circuits.
    for rule in tax["non_actionable"]:
        if re.search(rule["match"], hay, re.I | re.M):
            return _noise(rule)

    if _is_empty(ticket):
        return _noise({"id": "empty-submission", "label": "Empty / metadata-only submission"})

    theme = next((t for t in tax["themes"] if re.search(t["match"], hay, re.I | re.M)), None)
    if theme is None:
        # Soft noise runs only here, after every theme has missed. Praise, vague
        # rants and clinical complaints often sit alongside a real defect, so
        # they must never pre-empt a theme match.
        for rule in tax.get("non_actionable_fallback", []):
            if re.search(rule["match"], hay, re.I | re.M):
                return _noise(rule)
        return {**row, "area": "Unclassified", "theme": "unclassified",
                "theme_label": "Unclassified", "type": _type_of(hay),
                "severity": _severity(hay, None)[0], "severity_source": _severity(hay, None)[1]}

    sub = next(
        (s for s in theme.get("subthemes", []) if re.search(s["match"], hay, re.I | re.M)),
        None,
    )
    return {
        **row,
        "area": theme["area"],
        "theme": theme["id"],
        "theme_label": theme["label"],
        # "other" is a real answer, not a gap to be padded away. A theme whose
        # Other bucket dominates is a theme whose label is lying about it.
        "subtheme": sub["id"] if sub else "other",
        "subtheme_label": sub["label"] if sub else "Other / not broken out",
        "type": _type_of(hay),
        "severity": _severity(hay, theme)[0],
        "severity_source": _severity(hay, theme)[1],
    }


def _platform(ticket: dict) -> str:
    blob = (ticket.get("os", "") + " " + " ".join(ticket.get("labels", []))).lower()
    if "ios" in blob or "iphone" in blob or "ipad" in blob:
        return "iOS"
    if "android" in blob:
        return "Android"
    return "unknown"


def _type_of(hay: str) -> str:
    if FEATURE.search(hay):
        return "FEATURE_REQUEST"
    if DEGRADING.search(hay) or BLOCKING.search(hay):
        return "DEFECT"
    if DISSATISFIED.search(hay):
        return "DISSATISFACTION"
    return "DEFECT"


def _severity(hay: str, theme) -> tuple:
    """Return (severity, source).

    The floor exists because some themes are blocking by definition -- if the
    office has no record of the booking, the patient lost the appointment
    whether or not they used the word. But the floor decides most of the
    blocking count, so it has to be visible: "evidenced" means the ticket text
    said so, "inferred" means the theme asserted it.
    """
    if BLOCKING.search(hay):
        return "BLOCKS_BOOKING", "text"
    if theme and theme.get("severity_floor") == "BLOCKS_BOOKING":
        return "BLOCKS_BOOKING", "theme_floor"
    if DEGRADING.search(hay):
        return "DEGRADES", "text"
    return "ANNOYANCE", "text"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickets", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--taxonomy", default=None)
    args = ap.parse_args()

    tax = load_taxonomy(args.taxonomy) if args.taxonomy else load_taxonomy()
    with open(args.tickets) as fh:
        tickets = json.load(fh)

    rows = [classify_one(t, tax) for t in tickets]
    with open(args.output, "w") as fh:
        json.dump(rows, fh, indent=2)

    unc = sum(1 for r in rows if r["theme"] == "unclassified")
    pct = (100 * unc / len(rows)) if rows else 0
    print(f"classified {len(rows)} tickets -> {args.output}")
    print(f"unclassified: {unc} ({pct:.0f}%) -- extend config/taxonomy.json if this climbs")


if __name__ == "__main__":
    main()
