"""Query the ESGF-NG STAC index for CMIP7 datasets."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

P = "cmip7:"


@dataclass
class RemoteFile:
    name: str
    urls: list[str]
    size: int | None
    checksum: str | None        # multihash hex string as published (1220... = sha2-256)
    tracking_id: str | None
    created: str | None
    esgf_path: str | None

    @property
    def sha256(self) -> str | None:
        """Plain sha256 hex digest, if the published checksum is sha2-256."""
        c = (self.checksum or "").lower()
        if len(c) == 68 and c.startswith("1220"):
            return c[4:]
        if len(c) == 64:
            return c
        return None


@dataclass
class RemoteDataset:
    base_id: str
    version: str
    latest: bool
    retracted: bool
    activity: str
    institution: str
    source: str
    experiment: str
    member: str
    region: str
    frequency: str
    realm: str
    variable_id: str
    branded: str
    grid: str
    size: int | None
    long_name: str = ""
    units: str = ""
    files: list[RemoteFile] = field(default_factory=list)

    @property
    def dataset_id(self) -> str:
        return f"{self.base_id}.v{self.version}"


def _parse_item(item: dict) -> RemoteDataset:
    p = item["properties"]
    realm = p.get(P + "realm") or [""]
    files = []
    for name, a in (item.get("assets") or {}).items():
        if "data" not in (a.get("roles") or []) and not name.endswith(".nc"):
            continue
        urls = [a["href"]]
        for alt in (a.get("alternate") or {}).values():
            if isinstance(alt, dict) and alt.get("href") and alt["href"] not in urls:
                urls.append(alt["href"])
        files.append(RemoteFile(
            name=name if name.endswith(".nc") else a["href"].rsplit("/", 1)[-1],
            urls=urls,
            size=a.get("file:size"),
            checksum=a.get("file:checksum"),
            tracking_id=a.get(P + "tracking_id"),
            created=a.get("created"),
            esgf_path=a.get("file:local_path"),
        ))
    files.sort(key=lambda f: f.name)
    return RemoteDataset(
        # some items lack base_id; it is the item id without the trailing .vYYYYMMDD
        base_id=p.get("base_id") or item["id"].rsplit(".v", 1)[0],
        version=str(p["version"]),
        latest=bool(p.get("latest", True)),
        retracted=bool(p.get("retracted", False)),
        activity=p.get(P + "activity_id", ""),
        institution=p.get(P + "institution_id", ""),
        source=p[P + "source_id"],
        experiment=p[P + "experiment_id"],
        member=p[P + "variant_label"],
        region=p.get(P + "region", "glb"),
        frequency=p[P + "frequency"],
        realm=realm[0] if isinstance(realm, list) else realm,
        variable_id=p[P + "variable_id"],
        branded=p.get(P + "variable_branded_name") or f'{p[P + "variable_id"]}_{p[P + "variable_branding_suffix"]}',
        grid=p[P + "grid_label"],
        size=p.get("size"),
        long_name=p.get(P + "variable_long_name", ""),
        units=p.get(P + "variable_units", ""),
        files=files,
    )


def _get(session: requests.Session, url: str, params=None, retries: int = 4) -> dict:
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=300)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == retries - 1:
                raise
            wait = 10 * (attempt + 1)
            log.warning("STAC request failed (%s); retrying in %ss", e, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def search(settings, experiment: str, frequency: str, variable_id: str, region: str | None = None) -> list[RemoteDataset]:
    """Return every dataset version (latest, old and retracted) matching the query."""
    clauses = [
        f"\"{P}experiment_id\"='{experiment}'",
        f"\"{P}frequency\"='{frequency}'",
        f"\"{P}variable_id\"='{variable_id}'",
    ]
    if region:
        clauses.append(f"\"{P}region\"='{region}'")
    params = {
        "filter": " AND ".join(clauses),
        "filter-lang": "cql2-text",
        "limit": settings.stac_page_limit,
    }
    url = f"{settings.stac_url}/collections/{settings.stac_collection}/items"
    out: list[RemoteDataset] = []
    with requests.Session() as s:
        s.headers["User-Agent"] = "cmip7arch"
        while url:
            d = _get(s, url, params)
            for item in d.get("features", []):
                try:
                    out.append(_parse_item(item))
                except KeyError as e:
                    log.warning("skipping malformed item %s (missing %s)", item.get("id"), e)
            nxt = [l["href"] for l in d.get("links", []) if l.get("rel") == "next"]
            url, params = (nxt[0], None) if nxt else (None, None)
    log.info("STAC: %s %s %s %s -> %d dataset versions", experiment, frequency, variable_id, region or "", len(out))
    return out
