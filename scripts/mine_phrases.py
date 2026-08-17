#!/usr/bin/env python3
"""
mine_phrases.py -- local authoring aid for extending the taxonomy.

NOT part of the pipeline. Nothing this prints is publishable as-is.

Reads the PHI workdir and prints the phrases that recur inside a theme, so you
can see what patients are actually reporting and write a sub-theme regex that
matches it. The alternative -- reading ticket bodies yourself -- means reading
patient free text, which this exists to avoid.

Counts are by *distinct ticket*, and a phrase must appear in at least
--min-tickets of them before it is printed. A phrase carried by one or two
patients is that patient's wording; a phrase carried by six is product
vocabulary. Proper nouns and digits are dropped for the same reason.

    python3 scripts/mine_phrases.py --workdir work --theme insurance-network-accuracy
    python3 scripts/mine_phrases.py --workdir work --unclassified
"""

import argparse
import json
import os
import re
from collections import Counter

STOP = set("""
a about after all also am an and any are as at be been before being but by can
cant come could did do does doesnt doing dont each even for from get gets getting
give go going got had has have having he her here him his how i if in into is it
its ive just like ll me more most much my no not now of off on once one only or
other our out over own re said same she should so some such than that the their
them then there these they this those through to too up us use used using very
was we well were what when where which while who why will with would you your
zocdoc app
""".split())

TOKEN = re.compile(r"[a-z]+")


def tokens(text: str):
    # Lowercase and strip punctuation, so smart quotes and possessives collapse
    # into the same token. Digits go entirely -- they are the most likely
    # carrier of anything identifying.
    return [t for t in TOKEN.findall(text.lower()) if len(t) > 1]


def proper_noun_ratio(text: str, gram: str) -> bool:
    """True if every word of the gram appears capitalised mid-sentence."""
    words = gram.split()
    for w in words:
        if re.search(rf"(?<![.!?]\s)(?<!^)\b{re.escape(w.capitalize())}\b", text):
            continue
        return False
    return True


def mine(bodies, n_range=(2, 4), min_tickets=5, top=40):
    grams = Counter()
    for body in bodies:
        toks = tokens(body)
        seen = set()
        for n in range(n_range[0], n_range[1] + 1):
            for i in range(len(toks) - n + 1):
                g = toks[i:i + n]
                if all(t in STOP for t in g):
                    continue
                if g[0] in STOP and g[-1] in STOP:
                    continue
                seen.add(" ".join(g))
        grams.update(seen)  # one vote per ticket, not per occurrence
    return [(g, c) for g, c in grams.most_common() if c >= min_tickets][:top]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True, help="dir holding tickets.json + classified.json")
    ap.add_argument("--theme", default=None, help="restrict to one theme id")
    ap.add_argument("--unclassified", action="store_true", help="mine the unclassified tail")
    ap.add_argument("--min-tickets", type=int, default=5)
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    tickets = {t["key"]: t for t in json.load(open(os.path.join(args.workdir, "tickets.json")))}
    rows = json.load(open(os.path.join(args.workdir, "classified.json")))

    if args.unclassified:
        wanted = [r for r in rows if r["theme"] == "unclassified"]
        label = "unclassified"
    elif args.theme:
        wanted = [r for r in rows if r["theme"] == args.theme]
        label = args.theme
    else:
        by_theme = Counter(r["theme"] for r in rows if r["type"] != "NON_ACTIONABLE")
        for t, c in by_theme.most_common():
            print(f"{c:4d}  {t}")
        return

    bodies = [tickets[r["key"]].get("body", "") for r in wanted if r["key"] in tickets]
    print(f"# {label}: {len(bodies)} tickets, phrases in >= {args.min_tickets} of them\n")
    for gram, count in mine(bodies, min_tickets=args.min_tickets, top=args.top):
        print(f"{count:4d}  {gram}")


if __name__ == "__main__":
    main()
