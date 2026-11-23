"""Load settings and the wishlist."""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    archive_root: Path
    retracted_dir: Path
    incoming_dir: Path
    stac_url: str
    stac_collection: str
    stac_page_limit: int
    download_workers: int
    download_retries: int
    download_timeout: int
    insecure_ssl_hosts: set
    realm_prefix: dict
    catalog_dir: Path
    site_dir: Path


def load_settings(path: Path | None = None) -> Settings:
    path = Path(path or os.environ.get("CMIP7ARCH_SETTINGS", REPO_ROOT / "config" / "settings.yaml"))
    s = yaml.safe_load(path.read_text())
    root = Path(os.environ.get("CMIP7ARCH_ROOT", s["archive_root"]))
    return Settings(
        archive_root=root,
        retracted_dir=root / s.get("retracted_dir", "retracted"),
        incoming_dir=root / s.get("incoming_dir", "incoming"),
        stac_url=s["stac_url"].rstrip("/"),
        stac_collection=s.get("stac_collection", "CMIP7"),
        stac_page_limit=int(s.get("stac_page_limit", 100)),
        download_workers=int(s.get("download_workers", 4)),
        download_retries=int(s.get("download_retries", 3)),
        download_timeout=int(s.get("download_timeout", 300)),
        insecure_ssl_hosts=set(s.get("insecure_ssl_hosts") or []),
        realm_prefix=s.get("realm_prefix", {}),
        catalog_dir=REPO_ROOT / s.get("catalog_dir", "catalog"),
        site_dir=REPO_ROOT / s.get("site_dir", "docs"),
    )


@dataclass
class Request:
    experiments: list[str]
    frequency: str
    variables: list[str]           # branded names, may contain wildcards
    region: str = "glb"
    members: object = "all"        # "all" | int | list[str] | {experiment: spec, default: spec}
    models: list[str] | None = None
    exclude_models: list[str] = field(default_factory=list)

    def variable_roots(self) -> set[str]:
        """Root variable_ids to query ESGF with (the part before the first '_')."""
        return {v.split("_", 1)[0] for v in self.variables}

    def matches_branded(self, branded: str) -> bool:
        return any(fnmatch.fnmatchcase(branded, pat) for pat in self.variables)

    def members_for(self, experiment: str):
        """Member selection for one experiment ("all", an int, or a list of variant labels)."""
        m = self.members
        if isinstance(m, dict):
            m = m.get(experiment, m.get("default", "all"))
        return m

    def matches_model(self, source_id: str) -> bool:
        if self.models and source_id not in self.models:
            return False
        return source_id not in self.exclude_models


def _validate(n: int, r: dict) -> None:
    """Catch common wishlist mistakes with a readable message instead of a traceback."""
    where = f"wishlist request #{n}"
    for key in ("experiments", "frequency", "variables"):
        if key not in r:
            raise ValueError(f"{where}: missing '{key}'")
    exps = r["experiments"] if isinstance(r["experiments"], list) else [r["experiments"]]
    for name, vals in (("experiments", exps), ("variables", r["variables"])):
        if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
            raise ValueError(f"{where}: '{name}' must be a flat list of names, e.g. "
                             f"[a, b] or one '- name' per line (got {vals!r})")
    for v in r["variables"]:
        if "_" not in v:
            raise ValueError(f"{where}: '{v}' is not a branded name (<variable>_<branding>)")
    m = r.get("members", "all")
    specs = m.values() if isinstance(m, dict) else [m]
    for spec in specs:
        if not (spec == "all" or (isinstance(spec, int) and spec > 0)
                or (isinstance(spec, list) and all(isinstance(x, str) for x in spec))):
            raise ValueError(f"{where}: bad 'members' value {spec!r} (use all, N, or a list)")


def load_wishlist(path: Path | None = None) -> list[Request]:
    path = Path(path or os.environ.get("CMIP7ARCH_WISHLIST", REPO_ROOT / "config" / "wishlist.yaml"))
    w = yaml.safe_load(path.read_text()) or {}
    defaults = w.get("defaults") or {}
    out = []
    for n, r in enumerate(w.get("requests") or [], 1):
        merged = {**defaults, **r}
        # per-experiment member dicts in a request extend the defaults' dict
        if isinstance(defaults.get("members"), dict) and isinstance(r.get("members"), dict):
            merged["members"] = {**defaults["members"], **r["members"]}
        _validate(n, merged)
        exps = merged["experiments"]
        out.append(Request(
            experiments=[exps] if isinstance(exps, str) else list(exps),
            frequency=merged["frequency"],
            variables=list(merged["variables"]),
            region=merged.get("region", "glb"),
            members=merged.get("members", "all"),
            models=merged.get("models"),
            exclude_models=merged.get("exclude_models") or [],
        ))
    return out
