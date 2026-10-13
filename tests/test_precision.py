"""Grouping-precision gate, scored against a held-out split.

unclassified% only measures coverage -- a ticket matched *something*. It says
nothing about whether the theme it matched is the right one.

This file used to score `gold_set.tsv` and gate on 87%. That number was
circular: the same person wrote those labels and then tuned the regexes until
they passed. It measured memorisation, not accuracy.

So the labels are now split in two, and the split is frozen on disk:

  gold_set.tsv  -- TRAIN. Tune against this freely.
  test_set.tsv  -- HELD OUT. Frozen before the last tuning round. Never tune
                   against it; scoring it is the only number that means
                   anything, and it is the number the dashboard publishes.

The gap between the two is itself reported, because a widening gap is the
signal that tuning has started fitting the train labels rather than the
language patients actually use.

Both files store keys + theme only. No bodies, so no PHI.
"""
import json
import os
import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TRAIN = ROOT / "tests" / "data" / "gold_set.tsv"
TEST = ROOT / "tests" / "data" / "test_set.tsv"

# Ratchets, set just under the measured values so a real regression fails but
# ordinary label churn does not. Raise them as the taxonomy improves. Never
# lower one to make a red build green -- that is what made the old gate
# meaningless.
MIN_HELDOUT = 0.55          # measured 58.5%
MIN_HELDOUT_LENIENT = 0.60  # measured 64.6%, crediting unclassified as noise
MIN_TRAIN = 0.80            # measured 88%. NOT evidence of accuracy -- this is
                            # only here to catch a taxonomy edit that breaks
                            # even the cases it was written against.
MAX_OVERFIT_GAP = 0.35      # measured 0.30 (88% train vs 58% held out)

# The themes that drive the published ranking. Held-out recall on these is too
# small a sample to gate (n=5), but their *false positives* are gateable and are
# what would actually corrupt the ranking: a search/insurance theme that swallows
# unrelated tickets inflates its own published count.
CORE = {
    "insurance-network-accuracy",
    "insurance-plan-picker",
    "ai-search-rollout",
    "search-filter-correctness",
    "search-name-lookup",
    "search-no-results",
}


def _classified():
    """classified.json holds ticket bodies, so it lives in the PHI workdir and is
    never committed. CI sets AFH_WORKDIR (/tmp/afh) and shreds it after the run."""
    workdir = os.environ.get("AFH_WORKDIR")
    candidates = [pathlib.Path(workdir) / "classified.json"] if workdir else []
    candidates += [
        ROOT / ".afh-analysis" / "work" / "classified.json",
        pathlib.Path("/tmp/afh/classified.json"),
    ]
    for path in candidates:
        if path.exists():
            return {r["key"]: r for r in json.load(open(path))}
    pytest.skip("no classified.json; set AFH_WORKDIR or run fetch_data + classify")


def _labels(path):
    # Label files map ticket keys to themes -- patient-level, so they are
    # gitignored and only exist on the operator's machine.
    if not path.exists():
        pytest.skip(f"{path.name} is local-only; run the precision gate via run_month.sh")
    out = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("key\t"):
            continue
        key, theme = line.split("\t")[:2]
        theme = theme.strip()
        # Early labels recorded a noise subtype (NOISE:internal-artifact). The
        # classifier only ever says actionable-or-not, so the subtype is not
        # scoreable -- fold it down rather than counting it as a permanent miss.
        if theme.startswith("NOISE:"):
            theme = "noise"
        out[key.strip()] = theme
    return out


def _predict(row):
    """One label space for both sides, so 'noise' compares to 'noise'."""
    if row["type"] == "NON_ACTIONABLE":
        return "noise"
    return row.get("theme") or "unclassified"


def _score(labels, rows):
    hit = lenient = tot = 0
    misses = []
    for key, want in labels.items():
        if key not in rows:
            continue
        got = _predict(rows[key])
        ok = got == want
        tot += 1
        hit += ok
        # A noise ticket that matched no theme is a near-miss, not a
        # misgrouping: it lands in 'unclassified', which the report already
        # discloses rather than counting into a theme.
        lenient += ok or (want == "noise" and got == "unclassified")
        if not ok:
            misses.append(f"{key}: want {want}, got {got}")
    return hit, lenient, tot, misses


def test_splits_are_disjoint():
    """The whole point. If a key leaks from test into train it can be tuned
    against, and the held-out number silently becomes circular again."""
    overlap = set(_labels(TRAIN)) & set(_labels(TEST))
    assert not overlap, (
        f"{len(overlap)} key(s) in both train and test -- the held-out score is "
        f"no longer held out: {sorted(overlap)[:10]}"
    )


@pytest.mark.parametrize("path", [TRAIN, TEST], ids=["train", "test"])
def test_label_files_are_phi_free(path):
    """Neither file may ever grow a body column."""
    if not path.exists():
        pytest.skip(f"{path.name} is local-only")
    for line in path.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        assert len(line.split("\t")) == 2, f"must be 2 columns: {line[:60]}"


def test_heldout_precision():
    """The real gate."""
    hit, lenient, tot, misses = _score(_labels(TEST), _classified())
    assert tot >= 40, f"held-out sample too small to mean anything: {tot}"
    assert hit / tot >= MIN_HELDOUT, (
        f"held-out precision {hit}/{tot} = {hit / tot:.1%} < {MIN_HELDOUT:.0%}\n"
        + "\n".join(misses)
    )
    assert lenient / tot >= MIN_HELDOUT_LENIENT, (
        f"held-out lenient precision {lenient}/{tot} = {lenient / tot:.1%} "
        f"< {MIN_HELDOUT_LENIENT:.0%}"
    )


def test_train_precision_still_sane():
    """Not evidence of accuracy -- a floor under the cases the rules were
    written for. See MIN_TRAIN."""
    hit, _, tot, misses = _score(_labels(TRAIN), _classified())
    assert hit / tot >= MIN_TRAIN, (
        f"train precision {hit}/{tot} = {hit / tot:.1%} < {MIN_TRAIN:.0%}\n"
        + "\n".join(misses)
    )


def test_not_overfitting_further():
    """Train minus held-out. Tuning that lifts train without lifting held-out is
    fitting the labels, and this is the only test that can see it happening."""
    rows = _classified()
    th, _, tn, _ = _score(_labels(TRAIN), rows)
    eh, _, en, _ = _score(_labels(TEST), rows)
    gap = th / tn - eh / en
    assert gap <= MAX_OVERFIT_GAP, (
        f"overfit gap {gap:.0%} > {MAX_OVERFIT_GAP:.0%} "
        f"(train {th}/{tn} = {th / tn:.0%}, held out {eh}/{en} = {eh / en:.0%}). "
        "The last taxonomy edit fit the train labels without generalising."
    )


def test_core_themes_do_not_over_claim():
    """Core-theme false positives inflate the published ranking directly.

    Recall on these is measured too (0/5 held out -- they are *under*-called, so
    their counts read as floors), but 5 tickets is not a gate. Precision is:
    every held-out ticket assigned a core theme must actually be one.
    """
    rows = _classified()
    bad = []
    for key, want in _labels(TEST).items():
        if key not in rows:
            continue
        got = _predict(rows[key])
        if got in CORE and got != want:
            bad.append(f"{key}: called {got}, actually {want}")
    assert not bad, "core themes swallowed unrelated tickets:\n" + "\n".join(bad)
