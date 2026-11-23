"""Query the ESGF-NG STAC index for CMIP7 datasets."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

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
    globus: list[str] = field(default_factory=list)   # Globus web-app links, if published

    @property
    def dataset_id(self) -> str:
        return f"{self.base_id}.v{self.version}"


def _parse_item(item: dict) -> RemoteDataset:
    p = item["properties"]
    realm = p.get(P + "realm") or [""]
    files, globus = [], []
    for name, a in (item.get("assets") or {}).items():
        if a.get("protocol") == "globus" or "app.globus.org" in a.get("href", ""):
            # dataset-level Globus web-app link: record it, it is not a file
            globus.append(a["href"])
            continue
        if not a.get("href", "").endswith(".nc"):
            continue
        urls = [a["href"]]
        for alt in (a.get("alternate") or {}).values():
            if isinstance(alt, dict) and alt.get("href") and alt["href"] not in urls:
                urls.append(alt["href"])
        files.append(RemoteFile(
            name=a["href"].rsplit("/", 1)[-1],
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
        globus=globus,
    )


class IndexUnavailable(RuntimeError):
    """The ESGF index could not be queried (after retries)."""


# Backoff schedule (seconds) between attempts; ~10 minutes in total. The CEDA index
# intermittently drops TLS connections, especially after bursts of requests.
BACKOFF = [15, 30, 60, 120, 180, 240]


class _Client:
    """One polite, sequential HTTP client for the index: a single keep-alive session,
    a minimum interval between requests, Retry-After support and backoff."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "cmip7arch"
        self._last = 0.0

    def get(self, url: str, params=None) -> dict:
        for attempt in range(len(BACKOFF) + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            retry_after = None
            try:
                self._last = time.monotonic()
                r = self.session.get(url, params=params, timeout=300)
                if r.status_code in (429, 503):
                    retry_after = r.headers.get("Retry-After")
                r.raise_for_status()
                return r.json()
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if 400 <= code < 500 and code != 429:
                    raise                             # a bad query: retrying won't help
                err = e
            except (requests.RequestException, ValueError) as e:
                err = e
            if attempt == len(BACKOFF):
                raise IndexUnavailable(f"ESGF index unreachable after {attempt + 1} attempts: {err}")
            wait = BACKOFF[attempt]
            if retry_after and retry_after.isdigit():
                wait = max(wait, int(retry_after))
            log.warning("index request failed (%s); retrying in %ss", type(err).__name__, wait)
            time.sleep(wait)
        raise RuntimeError("unreachable")


_client: _Client | None = None


def _get_client(settings) -> _Client:
    global _client
    if _client is None:
        _client = _Client(settings.stac_min_interval)
    return _client


def _quote_list(vals) -> str:
    return ", ".join(f"'{v}'" for v in vals)


def _cache_file(settings, params: dict) -> Path:
    key = json.dumps({"url": settings.stac_url, "coll": settings.stac_collection, **params}, sort_keys=True)
    return settings.cache_dir / f"stac_{hashlib.sha1(key.encode()).hexdigest()[:16]}.json"


def search(settings, experiment: str, frequency: str, variable_ids, region: str | None = None,
           refresh: bool = False) -> list[RemoteDataset]:
    """Every dataset version (latest, old and retracted) for these variables in one experiment.

    variable_ids may be a single root name or a list; a list is sent as one query
    (variable_id IN (...)) to keep the number of requests to the index small.
    Results are cached for settings.stac_cache_hours (refresh=True bypasses the cache).
    """
    vids = [variable_ids] if isinstance(variable_ids, str) else sorted(variable_ids)
    clauses = [
        f"\"{P}experiment_id\"='{experiment}'",
        f"\"{P}frequency\"='{frequency}'",
        (f"\"{P}variable_id\"='{vids[0]}'" if len(vids) == 1
         else f"\"{P}variable_id\" IN ({_quote_list(vids)})"),
    ]
    if region:
        clauses.append(f"\"{P}region\"='{region}'")
    params = {
        "filter": " AND ".join(clauses),
        "filter-lang": "cql2-text",
        "limit": settings.stac_page_limit,
    }
    label = f"{experiment} {frequency} [{','.join(vids)}] {region or ''}"

    cache = _cache_file(settings, params)
    items = None
    if not refresh and cache.exists():
        age_h = (time.time() - cache.stat().st_mtime) / 3600
        if age_h < settings.stac_cache_hours:
            items = json.loads(cache.read_text())
            log.info("index (cached %.1fh ago): %s -> %d dataset versions", age_h, label, len(items))
    if items is None:
        client = _get_client(settings)
        url = f"{settings.stac_url}/collections/{settings.stac_collection}/items"
        items = []
        while url:
            d = client.get(url, params)
            items.extend(d.get("features", []))
            nxt = [l["href"] for l in d.get("links", []) if l.get("rel") == "next"]
            url, params = (nxt[0], None) if nxt else (None, None)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(items))
        log.info("index: %s -> %d dataset versions", label, len(items))

    out: list[RemoteDataset] = []
    for item in items:
        try:
            out.append(_parse_item(item))
        except KeyError as e:
            log.warning("skipping malformed item %s (missing %s)", item.get("id"), e)
    return out
