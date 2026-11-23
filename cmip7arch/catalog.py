"""Write catalog files (status.json + CSV) describing ESGF availability vs local holdings."""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from .archive import LocalDataset, PlanItem
from .config import Settings

FIELDS = ["experiment", "table", "variable", "source_id", "member", "grid", "status",
          "esgf_version", "local_version", "nfiles", "size_bytes", "institution", "activity",
          "long_name", "units", "path", "error"]


def _row(settings: Settings, rel: Path, status: str, remote=None, local: LocalDataset | None = None) -> dict:
    exp, table, var, src, mem, grid = rel.parts
    return {
        "experiment": exp, "table": table, "variable": var, "source_id": src, "member": mem, "grid": grid,
        "status": status,
        "esgf_version": remote.version if remote else "",
        "local_version": (local.version or "") if local else "",
        "nfiles": len(remote.files) if remote else (len(local.files) if local else 0),
        "size_bytes": (remote.size or sum(f.size or 0 for f in remote.files)) if remote else "",
        "institution": remote.institution if remote else "",
        "activity": remote.activity if remote else "",
        "long_name": remote.long_name if remote else "",
        "units": remote.units if remote else "",
        "error": "",
        "path": str((settings.retracted_dir if status == "retracted" else settings.archive_root) / rel),
    }


def _previous_errors(settings: Settings) -> dict:
    """Errors recorded by the last update run (kept so `check` doesn't erase them)."""
    p = settings.catalog_dir / "status.json"
    if not p.exists():
        return {}
    try:
        old = json.loads(p.read_text())["datasets"]
    except (ValueError, KeyError):
        return {}
    return {(r["experiment"], r["table"], r["variable"], r["source_id"], r["member"], r["grid"]): r.get("error", "")
            for r in old if r.get("error")}


def build_rows(settings: Settings, items: list[PlanItem], local: dict, retracted: dict,
               errors: dict | None = None) -> list[dict]:
    """errors: relpath -> messages from this update run; None/{} keeps the previous run's errors."""
    prev = _previous_errors(settings) if not errors else {}
    rows, seen = [], set()
    for it in items:
        if it.status == "retracted":
            continue
        # After a download run, re-derive the status from the fresh local scan
        loc = local.get(it.relpath)
        status = it.status
        if it.remote is not None:
            if loc is None:
                status = "available"
            elif loc.version != it.remote.version or any(
                    loc.files.get(f.name) is None for f in it.remote.files):
                status = "outdated" if loc.version and loc.version < it.remote.version else "incomplete"
            else:
                status = "downloaded"
        row = _row(settings, it.relpath, status, it.remote, loc)
        if status != "downloaded":
            if errors and it.relpath in errors:
                row["error"] = "; ".join(errors[it.relpath])[:500]
            else:
                row["error"] = prev.get(tuple(it.relpath.parts), "")
        rows.append(row)
        seen.add(it.relpath)
    for rel, loc in sorted(local.items()):
        if rel not in seen:
            rows.append(_row(settings, rel, "local-only", None, loc))
    for rel, loc in sorted(retracted.items()):
        rows.append(_row(settings, rel, "retracted", None, loc))
    return rows


def write(settings: Settings, rows: list[dict], requests_summary: list[dict]) -> Path:
    settings.catalog_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "archive_root": str(settings.archive_root),
        "stac_url": settings.stac_url,
        "wishlist": requests_summary,
        "datasets": rows,
    }
    out = settings.catalog_dir / "status.json"
    out.write_text(json.dumps(status, indent=1))
    with (settings.catalog_dir / "datasets.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return out
