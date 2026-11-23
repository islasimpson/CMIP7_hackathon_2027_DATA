"""Fetch files over HTTPS with checksum verification and install them in the archive."""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from .archive import PlanItem, append_info, info_path, read_info
from .config import Settings
from .stac import RemoteFile

log = logging.getLogger(__name__)
CHUNK = 8 * 1024 * 1024


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def _verify(path: Path, rf: RemoteFile) -> tuple[bool, str]:
    if not path.exists():
        return False, ""
    if rf.size is not None and path.stat().st_size != rf.size:
        return False, ""
    digest = sha256_of(path)
    if rf.sha256 and digest != rf.sha256:
        return False, digest
    return True, digest


def fetch_file(rf: RemoteFile, dest: Path, settings: Settings) -> str:
    """Download rf to dest (via dest.part), verify, return sha256. Reuses a verified dest."""
    ok, digest = _verify(dest, rf)
    if ok:
        return digest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last_err = None
    for attempt in range(settings.download_retries):
        for url in rf.urls:
            host = url.split("/")[2].split(":")[0]
            # Hosts listed in insecure_ssl_hosts (e.g. expired certificates) are fetched without
            # certificate verification, but only when a published sha256 exists to verify against.
            verify = not (host in settings.insecure_ssl_hosts and rf.sha256)
            try:
                if not verify:
                    log.info("  %s: fetching from %s without SSL certificate verification "
                             "(insecure_ssl_hosts); integrity checked by sha256", rf.name, host)
                h = hashlib.sha256()
                with requests.get(url, stream=True, timeout=settings.download_timeout, verify=verify,
                                  headers={"User-Agent": "cmip7arch"}) as r:
                    r.raise_for_status()
                    with part.open("wb") as f:
                        for b in r.iter_content(CHUNK):
                            f.write(b)
                            h.update(b)
                digest = h.hexdigest()
                if rf.size is not None and part.stat().st_size != rf.size:
                    raise IOError(f"size mismatch {part.stat().st_size} != {rf.size}")
                if rf.sha256 and digest != rf.sha256:
                    raise IOError("checksum mismatch")
                if not verify and digest != rf.sha256:  # belt and braces: never accept unverified
                    raise IOError("unverified download without checksum")
                part.replace(dest)
                return digest
            except Exception as e:  # noqa: BLE001 - try next mirror / retry
                last_err = e
                log.warning("  %s from %s failed: %s", rf.name, url.split("/")[2], e)
                part.unlink(missing_ok=True)
        time.sleep(15 * (attempt + 1))
    raise IOError(f"all attempts failed ({last_err})")


def _install(item: PlanItem, settings: Settings, staged: dict[str, tuple[Path, str]]) -> None:
    """Move verified staged files into the archive, drop superseded files, write .info rows."""
    ds = item.remote
    dest_dir = settings.archive_root / item.relpath
    dest_dir.mkdir(parents=True, exist_ok=True)
    new_names = {f.name for f in ds.files}

    # Remove files from an older version that are not part of the new version
    for old in dest_dir.glob("*.nc"):
        if old.name not in new_names:
            log.info("  removing superseded %s", old.name)
            with (dest_dir / ".history").open("a") as h:
                rows = read_info(old)
                v = rows[-1].version if rows else "unknown"
                h.write(f"{dt.datetime.now():%Y%m%d%H%M%S} removed {old.name} (v{v}) superseded by v{ds.version}\n")
            old.unlink()
            info_path(old).unlink(missing_ok=True)

    for f in ds.files:
        target = dest_dir / f.name
        rows = read_info(target) if target.exists() else []
        cur = rows[-1] if rows else None
        if f.name in staged:
            src, digest = staged[f.name]
            shutil.move(str(src), target)
        else:
            digest = cur.sha256 if cur else sha256_of(target)
        if cur is None or cur.version != ds.version or cur.sha256 != digest:
            append_info(target, f.created, ds.version, digest, f.tracking_id, f.esgf_path)


def run(items: list[PlanItem], settings: Settings, dry_run: bool = False) -> dict:
    todo = [i for i in items if i.status in ("new", "update", "incomplete")]
    stats = {"datasets": len(todo), "files": 0, "bytes": 0, "failed": [], "installed": 0,
             "errors": {}}

    # Files already present locally with the right checksum don't need fetching (common
    # when a new version only changes some files).
    jobs: list[tuple[PlanItem, RemoteFile]] = []
    for it in todo:
        for f in it.missing:
            local_row = it.local.files.get(f.name) if it.local else None
            if local_row and f.sha256 and local_row.sha256 == f.sha256:
                continue
            jobs.append((it, f))
    stats["files"] = len(jobs)
    stats["bytes"] = sum(f.size or 0 for _, f in jobs)
    if dry_run:
        return stats

    staged: dict[Path, dict[str, tuple[Path, str]]] = {it.relpath: {} for it in todo}
    failed: set[Path] = set()
    with ThreadPoolExecutor(settings.download_workers) as pool:
        futs = {}
        for it, f in jobs:
            dest = settings.incoming_dir / it.relpath / f"v{it.remote.version}" / f.name
            futs[pool.submit(fetch_file, f, dest, settings)] = (it, f, dest)
        for n, fut in enumerate(as_completed(futs), 1):
            it, f, dest = futs[fut]
            try:
                digest = fut.result()
                staged[it.relpath][f.name] = (dest, digest)
                log.info("[%d/%d] ok  %s", n, len(futs), f.name)
            except Exception as e:  # noqa: BLE001
                failed.add(it.relpath)
                stats["errors"].setdefault(it.relpath, []).append(f"{f.name}: {e}")
                stats["failed"].append(f"{f.name}: {e}")
                log.error("[%d/%d] FAILED %s: %s", n, len(futs), f.name, e)

    for it in todo:
        if it.relpath in failed:
            log.error("not installing %s (download failures; will retry next run)", it.relpath)
            continue
        _install(it, settings, staged[it.relpath])
        stats["installed"] += 1
        stage_dir = settings.incoming_dir / it.relpath
        shutil.rmtree(stage_dir, ignore_errors=True)
        _prune_empty(stage_dir.parent, settings.incoming_dir)
    return stats


def _prune_empty(d: Path, stop: Path) -> None:
    while d != stop and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


def move_retracted(items: list[PlanItem], settings: Settings, dry_run: bool = False) -> list[Path]:
    moved = []
    for it in items:
        if it.status != "retracted":
            continue
        src = settings.archive_root / it.relpath
        dst = settings.retracted_dir / it.relpath
        log.warning("dataset retracted on ESGF: %s (v%s) -> %s", it.relpath, it.local.version, dst)
        if not dry_run:
            if dst.exists():
                shutil.rmtree(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dst)
            _prune_empty(src.parent, settings.archive_root)
            (dst / "RETRACTED").write_text(
                f"Version v{it.local.version} was retracted on ESGF; moved here {dt.datetime.now():%Y-%m-%d %H:%M}\n")
        moved.append(it.relpath)
    return moved
