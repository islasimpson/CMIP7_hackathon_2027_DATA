"""Command line interface.

    cmip7arch check      query ESGF, compare with the archive, update catalog + site (no downloads)
    cmip7arch update     check + download new/updated data + move retracted data aside + site
    cmip7arch site       rebuild docs/index.html from catalog/status.json
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
from dataclasses import asdict

from . import archive, catalog, download, site, stac
from .config import load_settings, load_wishlist

log = logging.getLogger("cmip7arch")


def _query(settings, reqs, refresh=False):
    """One index query per (experiment, frequency, region), covering all variables."""
    groups: dict[tuple, set] = {}
    for r in reqs:
        for exp in r.experiments:
            groups.setdefault((exp, r.frequency, r.region), set()).update(r.variable_roots())
    remote = []
    for (exp, freq, region), roots in sorted(groups.items()):
        remote.extend(stac.search(settings, exp, freq, sorted(roots), region, refresh=refresh))
    return remote


def _summary(items):
    from collections import Counter
    c = Counter(i.status for i in items)
    return ", ".join(f"{k}: {v}" for k, v in sorted(c.items())) or "nothing matched"


def _gb(n):
    return f"{n / 1024**3:.1f} GB"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cmip7arch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "update", "site"])
    ap.add_argument("--dry-run", action="store_true", help="for update: report what would be fetched/moved")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore cached index results and query ESGF again")
    ap.add_argument("--no-site", action="store_true", help="don't rebuild the website")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    os.umask(0o022)  # files 644, dirs 755: readable by tutorial participants

    settings = load_settings()
    if a.command == "site":
        print(site.build(settings))
        return 0

    # Only one check/update at a time (e.g. a scheduled run overlapping a manual one)
    settings.archive_root.mkdir(parents=True, exist_ok=True)
    lock = open(settings.archive_root / ".cmip7arch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("another cmip7arch check/update is running (lock %s); exiting", lock.name)
        return 4
    lock.write(f"{os.getpid()} {os.uname().nodename}\n")
    lock.flush()

    try:
        reqs = load_wishlist()
    except ValueError as e:
        log.error("%s", e)
        return 2
    try:
        remote = _query(settings, reqs, refresh=a.refresh)
    except stac.IndexUnavailable as e:
        # Don't write a partial catalog/site or act on incomplete information.
        log.error("%s\nNothing was changed; try again later.", e)
        return 3
    local = archive.scan_local(settings)
    items = archive.plan(settings, reqs, remote, local)
    log.info("plan: %s", _summary(items))

    if a.command == "update":
        stats = download.run(items, settings, dry_run=a.dry_run)
        log.info("%s %d files (%s) for %d datasets; %d datasets installed",
                 "would fetch" if a.dry_run else "attempted", stats["files"], _gb(stats["bytes"]),
                 stats["datasets"], stats["installed"])
        moved = download.move_retracted(items, settings, dry_run=a.dry_run)
        if moved:
            log.warning("%s %d retracted datasets", "would move" if a.dry_run else "moved", len(moved))
        if stats.get("failed"):
            log.error("%d file downloads failed; rerun to retry:\n  %s", len(stats["failed"]),
                      "\n  ".join(stats["failed"][:20]))
        if not a.dry_run:
            local = archive.scan_local(settings)

    errors = stats["errors"] if a.command == "update" else {}
    rows = catalog.build_rows(settings, items, local, archive.scan_retracted(settings), errors)
    out = catalog.write(settings, rows, [{k: v for k, v in asdict(r).items()} for r in reqs])
    log.info("catalog written: %s", out)
    if not a.no_site:
        log.info("site written: %s", site.build(settings))
    pending = [i for i in items if i.status in ("new", "update", "incomplete")]
    if a.command == "check" and pending:
        n = sum(len(i.missing) for i in pending)
        b = sum(f.size or 0 for i in pending for f in i.missing)
        log.info("%d datasets (%d files, %s) can be downloaded with `cmip7arch update`", len(pending), n, _gb(b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
