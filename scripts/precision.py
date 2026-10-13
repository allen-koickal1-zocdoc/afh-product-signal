#!/usr/bin/env python3
"""
precision.py -- measure grouping accuracy against the hand-labelled gold set.

The unclassified % tells you what the taxonomy missed. It says nothing about
whether the tickets it *did* place went to the right theme. This does.

Gold set is keys + theme only (tests/data/gold_set.tsv), so it carries no PHI
and can live in the repo. Bodies stay in the job-local workdir.
"""
import argparse, json, os, sys
from collections import Counter, defaultdict

def load_gold(path):
    gold = {}
    for line in open(path):
        line = line.rstrip("\n")
        if not line or line.startswith("#") or line.startswith("key\t"):
            continue
        k, t = line.split("\t")[:2]
        gold[k] = t
    return gold

def measure(classified, gold):
    rows = {c["key"]: c for c in classified}
    hits = misses = 0
    per_theme = defaultdict(lambda: [0, 0])   # gold theme -> [correct, total]
    wrong = []
    for key, want in gold.items():
        if key not in rows:
            continue
        got = rows[key]["theme"]
        if want.startswith("NOISE:"):
            # gold says this is not feedback at all; any actionable theme is wrong
            ok = rows[key]["type"] == "NON_ACTIONABLE"
            want_label = want.split(":", 1)[1]
        else:
            ok = (got == want)
            want_label = want
        per_theme[want_label][1] += 1
        if ok:
            hits += 1
            per_theme[want_label][0] += 1
        else:
            misses += 1
            wrong.append((key, want_label, got))
    return hits, misses, per_theme, wrong

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classified", required=True)
    ap.add_argument("--gold", default=os.path.join(
        os.path.dirname(__file__), "..", "tests", "data", "gold_set.tsv"))
    ap.add_argument("--min-precision", type=float, default=0.0,
                    help="exit non-zero below this, for CI")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    gold = load_gold(args.gold)
    classified = json.load(open(args.classified))
    hits, misses, per_theme, wrong = measure(classified, gold)
    total = hits + misses
    if not total:
        sys.exit("no gold-set tickets found in the classified input")
    precision = hits / total

    print(f"grouping precision: {hits}/{total} = {precision:.1%}  "
          f"(gold set covers {total} tickets)")
    if not args.quiet:
        print("\nper gold theme:")
        for theme, (ok, n) in sorted(per_theme.items(), key=lambda x: -x[1][1]):
            flag = "" if ok == n else "   <-- " + str(n - ok) + " wrong"
            print(f"  {theme:32s} {ok:3d}/{n:<3d}{flag}")
        if wrong:
            print("\nstill misgrouped:")
            for key, want, got in sorted(wrong):
                print(f"  {key:9s} should be {want:30s} got {got}")

    if args.min_precision and precision < args.min_precision:
        print(f"\nFAIL: precision {precision:.1%} below floor {args.min_precision:.1%}",
              file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
