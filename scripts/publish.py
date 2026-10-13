#!/usr/bin/env python3
"""
publish.py -- push a built dashboard to the gh-pages branch.

Called by the monthly workflow and by run_month.sh. Everything upstream touches
patient free text inside a shredded workdir; by the time anything reaches here
it has already passed assert_no_phi.py and is aggregates only.

gh-pages is the archive store, not the local disk. Each run clones the live
branch, overlays the newest report at the root, freezes a copy under
archive/<month>/, rebuilds the archive index from whatever months are actually
present, and pushes. That means the history survives a new laptop, and two
people can publish without one of them silently dropping the other's months.

Pushing is opt-in. Without --push the script builds the commit and stops, so
you can diff what would go out before anything leaves the machine.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assert_no_phi import scan_text  # noqa: E402
from generate_pages import _env, render_index  # noqa: E402

MONTH_DIR = re.compile(r"\d{4}-\d{2}")


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, text=True, capture_output=True
    )


def clone_pages(remote: str, branch: str, dest: Path) -> bool:
    """Clone the live branch. Returns False if it does not exist yet."""
    r = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", branch, remote, str(dest)],
        text=True,
        capture_output=True,
    )
    if r.returncode == 0:
        return True
    # First publish: no branch yet. Start an orphan so main's history stays out.
    dest.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=dest)
    git("remote", "add", "origin", remote, cwd=dest)
    git("checkout", "-q", "--orphan", branch, cwd=dest)
    return False


def overlay(pages: Path, site: Path, month: str):
    """Newest report at the root, plus a frozen permalink for this month."""
    for item in pages.iterdir():
        if item.name == "archive":
            continue
        target = site / item.name
        if item.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    snapshot = site / "archive" / month
    shutil.rmtree(snapshot, ignore_errors=True)
    snapshot.mkdir(parents=True)
    for item in pages.iterdir():
        if item.name == "archive":
            continue
        if item.is_dir():
            shutil.copytree(item, snapshot / item.name)
        else:
            shutil.copy2(item, snapshot / item.name)


def read_snapshots(archive: Path):
    """Summarise each archived month from its own frozen report.json.

    A month whose report.json is unreadable still gets a row -- the directory is
    reachable, so hiding it would make the index lie about what is published.
    """
    out = []
    for d in sorted(p for p in archive.iterdir() if p.is_dir() and MONTH_DIR.fullmatch(p.name)):
        row = {"month": d.name, "total": None, "actionable": None,
               "blocking": None, "top_theme": None, "generated_at": None}
        try:
            r = json.loads((d / "report.json").read_text())
            row.update(
                total=r["totals"]["tickets"],
                actionable=r["totals"]["actionable"],
                blocking=r["totals"]["blocking"],
                top_theme=r["themes"][0]["label"] if r["themes"] else None,
                generated_at=r.get("generated_at"),
            )
        except (OSError, ValueError, KeyError, IndexError):
            pass
        out.append(row)

    # Snapshots are cumulative, so the useful delta is between consecutive
    # publishes. A month whose report.json failed to parse breaks the chain
    # rather than producing a delta against the wrong baseline.
    for prev, cur in zip(out, out[1:]):
        cur["delta"] = (
            cur["actionable"] - prev["actionable"]
            if cur["actionable"] is not None and prev["actionable"] is not None
            else None
        )
    if out:
        out[0]["delta"] = None
    return out


def write_archive_index(site: Path, report: dict):
    archive = site / "archive"
    archive.mkdir(exist_ok=True)
    snapshots = read_snapshots(archive)
    html = _env(report).get_template("archive.html").render(
        report=report, snapshots=snapshots, depth="../"
    )
    (archive / "index.html").write_text(html)
    return snapshots


def guard(site: Path):
    """Re-scan the assembled tree, not just this month's build.

    The per-month PHI gate ran before overlay. This catches the case where an
    older archived month was published under a looser guard than today's.
    """
    problems, checked = [], 0
    for path in sorted(site.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        if path.suffix.lower() not in {".html", ".csv", ".json", ".txt", ".md"}:
            continue
        problems += scan_text(path)
        checked += 1
    if checked == 0:
        sys.exit("PHI guard FAILED — scanned 0 files in the assembled site.")
    if problems:
        print("PHI guard FAILED — refusing to publish:", file=sys.stderr)
        for p in problems[:40]:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    print(f"PHI guard passed on the assembled site: {checked} file(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", required=True, help="Locally built pages dir")
    ap.add_argument("--report", required=True, help="report.json for this run")
    ap.add_argument("--month", required=True, help="YYYY-MM this run reports on")
    ap.add_argument("--label", default="", help="Human label for the commit message")
    ap.add_argument("--remote", default="git@github.com:allen-koickal1-zocdoc/afh-product-signal.git")
    ap.add_argument("--branch", default="gh-pages")
    ap.add_argument("--push", action="store_true", help="Actually push. Off by default.")
    ap.add_argument("--keep", default=None, help="Copy the assembled site here for inspection")
    args = ap.parse_args()

    if not MONTH_DIR.fullmatch(args.month):
        sys.exit(f"--month must be YYYY-MM, got {args.month!r}")

    report = json.loads(Path(args.report).read_text())
    pages = Path(args.pages)
    if not (pages / "index.html").exists():
        sys.exit(f"no index.html in {pages} — run generate_pages.py first")

    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "site"
        existed = clone_pages(args.remote, args.branch, site)
        print(f"{args.branch}: {'cloned' if existed else 'does not exist yet — creating'}")

        # The month-by-month table can only link snapshots that exist. That set
        # lives on the branch, not in the local build, so the root page is
        # re-rendered here -- before the overlay, so the frozen copy matches.
        archived = {d.name for d in (site / "archive").iterdir()
                    if d.is_dir() and MONTH_DIR.fullmatch(d.name)} \
            if (site / "archive").is_dir() else set()
        archived.add(args.month)
        render_index(report, pages, archived)
        print(f"index links {len(archived)} archived month(s)")

        overlay(pages, site, args.month)
        snapshots = write_archive_index(site, report)
        # GitHub Pages runs Jekyll by default, which drops files starting with
        # an underscore and can rewrite paths. This is a plain static tree.
        (site / ".nojekyll").write_text("")
        guard(site)

        print(f"months in archive: {', '.join(s['month'] for s in snapshots)}")

        git("add", "-A", cwd=site)
        status = git("status", "--porcelain", cwd=site).stdout.strip()
        if not status:
            print("no changes — nothing to publish")
            return
        print(f"{len(status.splitlines())} file(s) changed")

        git("-c", "user.name=afh-product-signal",
            "-c", "user.email=allen.koickal@zocdoc.com",
            "commit", "-q", "-m",
            f"AFH product signal — {args.label or args.month}", cwd=site)

        if args.keep:
            shutil.rmtree(args.keep, ignore_errors=True)
            shutil.copytree(site, args.keep, ignore=shutil.ignore_patterns(".git"))
            print(f"assembled site copied to {args.keep}")

        if not args.push:
            print("\nDRY RUN — commit built but not pushed. Re-run with --push to publish.")
            return

        git("push", "origin", f"HEAD:{args.branch}", cwd=site)
        print(f"pushed to {args.branch}")


if __name__ == "__main__":
    main()
