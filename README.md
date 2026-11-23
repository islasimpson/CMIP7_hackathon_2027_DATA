# CMIP7_hackathon_2027_DATA

Tools for keeping a local CMIP7 archive on GLADE (`/glade/campaign/cgd/cas/CMIP7`) in sync
with ESGF, and a web page summarizing what is available and what has been downloaded.

**Website:** built into [`docs/`](docs/) and served with GitHub Pages
(Settings → Pages → Branch `main`, folder `/docs`).

## Archive layout

Like the CMIP6 archive at `/glade/campaign/cgd/cas/CMIP6`, but variables use the CMIP7
*branded* names:

```
CMIP7/<experiment>/<table>/<branded_variable>/<source_id>/<member>/<grid>/<file>.nc
CMIP7/historical/Amon/tas_tavg-h2m-hxy-u/CanESM6-0-MR/r1i1p1f1/g150/tas_tavg-h2m-hxy-u_mon_glb_g150_CanESM6-0-MR_historical_r1i1p1f1_185001-201412.nc
```

* `<table>` is built from realm + frequency (`atmos`+`mon` → `Amon`, `ocean`+`day` → `Oday`);
  data for a non-global region goes under `<table>-<region>`.
* Every file has a hidden `.<file>.info` provenance file (same format as CMIP6): creation date,
  version date, archive date, sha256, tracking id, ESGF path; one row per version.
* Only the latest ESGF version is kept. Files dropped by a new version are removed and noted
  in `.history` in that directory.
* Datasets retracted on ESGF are moved to `CMIP7/retracted/<same path>` (with a `RETRACTED`
  note) and flagged on the website.
* `CMIP7/incoming/` is the staging area for in-progress downloads.

## Usage

```bash
bin/cmip7arch check              # query ESGF, compare with the archive, update catalog + website
bin/cmip7arch update --dry-run   # show what would be downloaded / moved
bin/cmip7arch update             # download new + updated data, move retracted data aside
bin/cmip7arch site               # rebuild docs/index.html from catalog/status.json
```

`bin/cmip7arch` uses the NCAR NPL conda environment
(`/glade/u/apps/opt/conda/envs/npl/bin/python`); override with `CMIP7ARCH_PYTHON`.
Re-running is always safe: verified files are never re-downloaded, and failed downloads
are retried on the next run.

After an `update`, run `scripts/publish_site.sh` to commit and push `catalog/` and `docs/`
(this refreshes the website).

### Running a big update as a batch job

Large downloads are better run on a Casper compute node than on a login node:

```bash
qsub scripts/update.pbs                                  # update --refresh, then publish the site
qsub -v CMIP7ARCH_ARGS="--dry-run" scripts/update.pbs    # just report what would be fetched
```

The job is charged to project P04010022 and logs to `logs/update_<date>.log`. Only one
`check`/`update` can run at a time (lock file `CMIP7/.cmip7arch.lock`); a second one exits
straight away.

Index results are cached for 6 hours in `.cache/` (so `check` then `update` queries ESGF
once); add `--refresh` to force a fresh query.

## What to download: `config/wishlist.yaml`

```yaml
defaults:
  region: glb
  members: all          # or N (first N members per model) or [r1i1p1f1, ...]

requests:
  - experiments: [historical]
    frequency: mon
    variables: [tas_tavg-h2m-hxy-u]
  - experiments: [historical, piControl]
    frequency: day
    variables: ["pr_*"]  # wildcards select every branding
    members: 3
    exclude_models: [SomeModel]
```

Archive-wide settings (paths, ESGF index URL, number of parallel downloads) are in
`config/settings.yaml`.

## How it works

* **Search:** the ESGF-NG STAC index (`https://api.stac.esgf.ceda.ac.uk`, collection `CMIP7`)
  covers every data node. Each dataset version carries `latest`/`retracted` flags and
  per-file sizes and sha256 checksums.
* **Download:** HTTPS from the data node in the index (falling back to listed mirrors),
  verified against size and sha256, staged in `incoming/` and only moved into the archive
  once every file of the dataset has verified.
* **Catalog:** `catalog/status.json` and `catalog/datasets.csv` list every wishlist dataset
  with status `downloaded`, `available`, `outdated`, `incomplete`, `retracted` or `local-only`,
  plus any download error. The website is generated from these.

## Layout of this repo

```
bin/cmip7arch        wrapper script
cmip7arch/           python package (stac.py search, archive.py layout/planning,
                     download.py, catalog.py, site.py, cli.py)
config/              wishlist.yaml, settings.yaml
catalog/             generated status.json / datasets.csv
docs/                generated website (GitHub Pages)
```
