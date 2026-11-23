"""Local archive layout, provenance (.info) files, and ESGF-vs-local planning.

Layout (CMIP6-like, with CMIP7 branded variable names):

    <root>/<experiment>/<table>/<branded_variable>/<source_id>/<member>/<grid>/<file>.nc
    e.g. CMIP7/historical/Amon/tas_tavg-h2m-hxy-u/CanESM6-0-MR/r1i1p1f1/g150/tas_tavg-..._185001-201412.nc

<table> is <realm prefix><frequency> (Amon, Omon, SIday, ...); a non-global
region is appended as <table>-<region>. Every .nc file has a hidden
.<file>.info sidecar with one row per version of that file, in the same format
as the CMIP6 archive.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Request, Settings
from .stac import RemoteDataset

log = logging.getLogger(__name__)

INFO_HEADER = (
    "# columns: <creation date> <version date> <archive date> <sha256sum> <tracking id> <ESGF path>\n"
    "# rows:    reflect the file history; current file info listed in the last row\n"
)


# ---------------------------------------------------------------- layout
def table_name(settings: Settings, realm: str, frequency: str, region: str = "glb") -> str:
    prefix = settings.realm_prefix.get(realm, realm)
    t = f"{prefix}{frequency}"
    return t if region in ("glb", "", None) else f"{t}-{region}"


def dataset_relpath(settings: Settings, ds: RemoteDataset) -> Path:
    return Path(ds.experiment, table_name(settings, ds.realm, ds.frequency, ds.region),
                ds.branded, ds.source, ds.member, ds.grid)


def info_path(nc: Path) -> Path:
    return nc.with_name(f".{nc.stem}.info")


# ---------------------------------------------------------------- .info files
@dataclass
class InfoRow:
    created: str
    version: str          # YYYYMMDD
    archived: str
    sha256: str
    tracking_id: str
    esgf_path: str


def read_info(nc: Path) -> list[InfoRow]:
    p = info_path(nc)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 6:
            rows.append(InfoRow(parts[0], parts[1][:8], parts[2], parts[3], parts[4], parts[5]))
    return rows


def append_info(nc: Path, created: str | None, version: str, sha256: str, tracking_id: str | None,
                esgf_path: str | None) -> None:
    def stamp(s: str | None) -> str:
        if not s:
            return "00000000000000"
        return re.sub(r"\D", "", s)[:14].ljust(14, "0")

    p = info_path(nc)
    now = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    line = (f"{stamp(created)} {version}000000 {now} {sha256} {tracking_id or 'none'} "
            f"{esgf_path or 'none'} \n")
    with p.open("a") as f:
        if p.stat().st_size == 0:
            f.write(INFO_HEADER)
        f.write(line)


# ---------------------------------------------------------------- local scan
@dataclass
class LocalDataset:
    relpath: Path
    files: dict[str, InfoRow | None] = field(default_factory=dict)   # filename -> current info row

    @property
    def version(self) -> str | None:
        vs = {r.version for r in self.files.values() if r}
        return max(vs) if vs else None


def _scan_tree(base: Path) -> dict[Path, LocalDataset]:
    out: dict[Path, LocalDataset] = {}
    if not base.exists():
        return out
    # <exp>/<table>/<var>/<source>/<member>/<grid>/*.nc
    for nc in base.glob("*/*/*/*/*/*/*.nc"):
        rel = nc.parent.relative_to(base)
        ds = out.setdefault(rel, LocalDataset(rel))
        rows = read_info(nc)
        ds.files[nc.name] = rows[-1] if rows else None
    return out


def scan_local(settings: Settings) -> dict[Path, LocalDataset]:
    skip = {settings.retracted_dir.name, settings.incoming_dir.name}
    out = _scan_tree(settings.archive_root)
    return {k: v for k, v in out.items() if k.parts[0] not in skip}


def scan_retracted(settings: Settings) -> dict[Path, LocalDataset]:
    return _scan_tree(settings.retracted_dir)


# ---------------------------------------------------------------- planning
def _member_key(m: str):
    nums = re.findall(r"\d+", m)
    return tuple(int(n) for n in nums) or (m,)


@dataclass
class PlanItem:
    status: str                    # new | update | incomplete | ok | retracted
    relpath: Path
    remote: RemoteDataset | None   # dataset version we want locally (None for retracted)
    local: LocalDataset | None
    missing: list = field(default_factory=list)   # RemoteFile objects to fetch


def plan(settings: Settings, requests_: list[Request], remote_all: list[RemoteDataset],
         local: dict[Path, LocalDataset]) -> list[PlanItem]:
    """Compare what ESGF has (for the wishlist) against what's on disk."""
    # Group all versions by base_id
    by_base: dict[str, list[RemoteDataset]] = {}
    for ds in remote_all:
        by_base.setdefault(ds.base_id, []).append(ds)

    wanted: dict[Path, RemoteDataset] = {}
    retracted_versions: dict[Path, set[str]] = {}
    for req in requests_:
        cand: dict[tuple, list[RemoteDataset]] = {}
        for base, versions in by_base.items():
            v0 = versions[0]
            if (v0.experiment not in req.experiments or v0.frequency != req.frequency
                    or v0.region != req.region or not req.matches_branded(v0.branded)
                    or not req.matches_model(v0.source)):
                continue
            rel = dataset_relpath(settings, v0)
            retracted_versions.setdefault(rel, set()).update(v.version for v in versions if v.retracted)
            good = [v for v in versions if v.latest and not v.retracted and v.files]
            if not good:
                continue
            best = max(good, key=lambda v: v.version)
            cand.setdefault((best.experiment, best.branded, best.source), []).append(best)
        # member selection
        for key, dss in cand.items():
            dss.sort(key=lambda d: _member_key(d.member))
            sel = req.members_for(key[0])
            if isinstance(sel, int):
                dss = dss[:sel]
            elif isinstance(sel, list):
                dss = [d for d in dss if d.member in sel]
            for d in dss:
                wanted[dataset_relpath(settings, d)] = d

    items: list[PlanItem] = []
    for rel, ds in sorted(wanted.items()):
        loc = local.get(rel)
        if loc is None:
            items.append(PlanItem("new", rel, ds, None, list(ds.files)))
            continue
        lv = loc.version
        if lv is None or lv < ds.version:
            items.append(PlanItem("update", rel, ds, loc, list(ds.files)))
            continue
        missing = []
        for f in ds.files:
            row = loc.files.get(f.name)
            if row is None or (f.sha256 and row.sha256 != f.sha256):
                missing.append(f)
        items.append(PlanItem("incomplete" if missing else "ok", rel, ds, loc, missing))

    # Local datasets whose version has been retracted on ESGF and have no valid replacement
    for rel, loc in sorted(local.items()):
        if rel in wanted:
            continue
        if loc.version and loc.version in retracted_versions.get(rel, set()):
            items.append(PlanItem("retracted", rel, None, loc))
    return items
