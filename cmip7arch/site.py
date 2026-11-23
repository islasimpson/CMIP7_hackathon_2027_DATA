"""Render the GitHub Pages site (docs/index.html) from catalog/status.json."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from jinja2 import Environment

from .config import Settings

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CMIP7 Data Holdings</title>
<style>
:root{--bg:#fff;--fg:#1d2330;--muted:#5d6675;--line:#dfe3ea;--card:#f6f8fb;
 --ok:#1f7a3f;--okbg:#e3f4e8;--av:#8a5a00;--avbg:#fff1d6;--out:#a03a00;--outbg:#ffe4d6;--re:#9b1c31;--rebg:#fbe0e5;--lo:#4b5563;--lobg:#eceff3;--accent:#2457c5}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#12151b;--fg:#e6e9ef;--muted:#9aa3b2;--line:#2a303b;--card:#1a1f27;
 --ok:#7bd99a;--okbg:#173322;--av:#f2c46b;--avbg:#3a2d10;--out:#ffab7a;--outbg:#3d2214;--re:#ff8fa3;--rebg:#3d1520;--lo:#c2c8d2;--lobg:#262c36;--accent:#8fb0ff}}
:root[data-theme="dark"]{--bg:#12151b;--fg:#e6e9ef;--muted:#9aa3b2;--line:#2a303b;--card:#1a1f27;
 --ok:#7bd99a;--okbg:#173322;--av:#f2c46b;--avbg:#3a2d10;--out:#ffab7a;--outbg:#3d2214;--re:#ff8fa3;--rebg:#3d1520;--lo:#c2c8d2;--lobg:#262c36;--accent:#8fb0ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem;margin:0 0 4px}h2{font-size:1.25rem;margin:36px 0 8px}h3{font-size:1.05rem;margin:24px 0 6px}
.muted{color:var(--muted)}code{font:0.88em ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--card);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card b{display:block;font-size:1.4rem}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:0.9rem}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{background:var(--card);font-weight:600}tr:last-child td{border-bottom:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.b{display:inline-block;padding:1px 8px;border-radius:10px;font-size:0.8rem;font-weight:600}
.downloaded{color:var(--ok);background:var(--okbg)}.available{color:var(--av);background:var(--avbg)}
.outdated,.incomplete{color:var(--out);background:var(--outbg)}.retracted{color:var(--re);background:var(--rebg)}
.local-only{color:var(--lo);background:var(--lobg)}.failing{color:var(--re);background:var(--rebg);cursor:help}
details{margin:8px 0}summary{cursor:pointer;color:var(--accent)}
input[type=search]{width:100%;max-width:360px;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.legend span{margin-right:8px}
</style>
</head>
<body><main>
<h1>CMIP7 Data Holdings</h1>
<p class="muted">What CMIP7 data is on ESGF for our wishlist, and what is already on GLADE at
<code>{{ s.archive_root }}</code>. Updated {{ s.generated }}.</p>

<div class="cards">
 <div class="card"><b>{{ n.models }}</b>models available</div>
 <div class="card"><b>{{ n.downloaded }}</b>datasets downloaded</div>
 <div class="card"><b>{{ n.pending }}</b>datasets not yet downloaded</div>
 <div class="card"><b>{{ n.retracted }}</b>retracted</div>
 <div class="card"><b>{{ n.size }}</b>on disk (wishlist)</div>
</div>

<p class="legend"><span class="b downloaded">downloaded</span><span class="b available">available</span>
<span class="b outdated">outdated</span><span class="b incomplete">incomplete</span>
<span class="b retracted">retracted</span><span class="b local-only">local-only</span>
<span class="b failing">⚠ download failing</span> <span class="muted">(hover for details; usually a data-node problem on ESGF)</span></p>

<h2>Overview</h2>
<p class="muted">Number of models downloaded / available on ESGF, for each variable and experiment.</p>
<div class="scroll"><table>
<tr><th>Table</th><th>Variable</th>{% for e in experiments %}<th>{{ e }}</th>{% endfor %}</tr>
{% for (table, var), cols in overview %}
<tr><td>{{ table }}</td><td><a href="#{{ table }}-{{ var }}"><code>{{ var }}</code></a></td>
{% for e in experiments %}{% set c = cols.get(e) %}<td class="num">{% if c %}{{ c[0] }} / {{ c[1] }}{% else %}–{% endif %}</td>{% endfor %}</tr>
{% endfor %}
</table></div>

<h2>By variable</h2>
<input type="search" id="q" placeholder="Filter models…" oninput="filt(this.value)">
{% for (table, var), exps in groups %}
<h3 id="{{ table }}-{{ var }}">{{ table }} · <code>{{ var }}</code></h3>
{% if meta[(table, var)] %}<p class="muted">{{ meta[(table, var)] }}</p>{% endif %}
{% for exp, models in exps %}
<details open><summary>{{ exp }} — {{ models|length }} models</summary>
<div class="scroll"><table class="f">
<tr><th>Model</th><th>Institution</th><th>Grid</th><th class="num">Members (dl / avail)</th><th>Version(s)</th><th>Status</th></tr>
{% for m in models %}
<tr data-m="{{ m.source|lower }}"><td>{{ m.source }}</td><td>{{ m.institution }}</td><td>{{ m.grids }}</td>
<td class="num">{{ m.dl }} / {{ m.avail }}</td><td>{{ m.versions }}</td>
<td><span class="b {{ m.status }}">{{ m.status }}</span>{% if m.error %} <span class="b failing" title="{{ m.error }}">⚠ download failing</span>{% endif %}</td></tr>
{% endfor %}
</table></div>
<p class="muted">Path: <code>{{ s.archive_root }}/{{ exp }}/{{ table }}/{{ var }}/&lt;model&gt;/&lt;member&gt;/&lt;grid&gt;/</code></p>
</details>
{% endfor %}
{% endfor %}

{% if retracted %}
<h2>Retracted datasets</h2>
<p class="muted">These were downloaded but have since been retracted on ESGF. They are kept aside under
<code>{{ s.archive_root }}/retracted/</code> and should not be used.</p>
<div class="scroll"><table>
<tr><th>Experiment</th><th>Variable</th><th>Model</th><th>Member</th><th>Grid</th><th>Version</th></tr>
{% for r in retracted %}<tr><td>{{ r.experiment }}</td><td><code>{{ r.variable }}</code></td><td>{{ r.source_id }}</td><td>{{ r.member }}</td><td>{{ r.grid }}</td><td>{{ r.local_version }}</td></tr>{% endfor %}
</table></div>
{% endif %}

<h2>About CMIP7 variable names</h2>
<p>CMIP7 identifies variables by a <em>branded name</em>, <code>&lt;variable&gt;_&lt;branding&gt;</code>. The branding has four parts:
temporal (<code>tavg</code> mean, <code>tmaxavg</code> monthly mean of daily max, <code>tpt</code> instantaneous…),
vertical (<code>h2m</code> 2&nbsp;m height, <code>u</code> unspecified, <code>p19</code> 19 pressure levels…),
horizontal (<code>hxy</code> lat-lon field, <code>hy</code> zonal mean…) and area (<code>u</code> all, <code>sea</code>, <code>lnd</code>…).
For example, <code>tas_tavg-h2m-hxy-u</code> is the CMIP6 <code>Amon/tas</code> equivalent.
The <code>Amon</code>-style folders come from realm + frequency.</p>
<p class="muted">Data source: ESGF STAC index at <code>{{ s.stac_url }}</code>. Generated by
<code>cmip7arch</code>; the full list is in <a href="datasets.csv">datasets.csv</a>.</p>
</main>
<script>
function filt(q){q=q.toLowerCase();document.querySelectorAll('table.f tr[data-m]').forEach(function(r){r.style.display=r.dataset.m.indexOf(q)>=0?'':'none';});}
</script>
</body></html>
"""

ORDER = ["retracted", "outdated", "incomplete", "available", "downloaded", "local-only"]


def _hsize(n: float) -> str:
    for u in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if n < 1024:
            return f"{n:.1f} {u}" if u not in ("B", "KB") else f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} EB"


def build(settings: Settings) -> Path:
    status = json.loads((settings.catalog_dir / "status.json").read_text())
    rows = status["datasets"]
    live = [r for r in rows if r["status"] != "retracted"]

    experiments = sorted({r["experiment"] for r in live})
    grp: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    meta = {}
    for r in live:
        grp[(r["table"], r["variable"])][r["experiment"]][r["source_id"]].append(r)
        if r["long_name"]:
            meta[(r["table"], r["variable"])] = f'{r["long_name"]} [{r["units"]}]'

    groups, overview = [], []
    for key in sorted(grp):
        exps, cols = [], {}
        for exp in sorted(grp[key]):
            models = []
            for src in sorted(grp[key][exp], key=str.lower):
                rs = grp[key][exp][src]
                avail = sum(r["status"] != "local-only" for r in rs)
                dl = sum(r["status"] in ("downloaded", "local-only") for r in rs)
                worst = min((r["status"] for r in rs), key=ORDER.index)
                st = "downloaded" if dl == avail and avail else ("available" if dl == 0 else worst)
                models.append({
                    "source": src,
                    "institution": next((r["institution"] for r in rs if r["institution"]), ""),
                    "grids": ", ".join(sorted({r["grid"] for r in rs})),
                    "dl": dl, "avail": avail,
                    "versions": ", ".join(sorted({"v" + (r["esgf_version"] or r["local_version"]) for r in rs})),
                    "status": st,
                    "error": "\n".join(r["error"] for r in rs if r.get("error")),
                })
            exps.append((exp, models))
            cols[exp] = (sum(m["dl"] > 0 for m in models), sum(m["avail"] > 0 for m in models))
        groups.append((key, exps))
        overview.append((key, cols))

    n = {
        "models": len({r["source_id"] for r in live if r["status"] != "local-only"}),
        "downloaded": sum(r["status"] == "downloaded" for r in rows),
        "pending": sum(r["status"] in ("available", "outdated", "incomplete") for r in rows),
        "retracted": sum(r["status"] == "retracted" for r in rows),
        "size": _hsize(sum(int(r["size_bytes"] or 0) for r in rows if r["status"] == "downloaded")),
    }
    html = Environment(autoescape=True).from_string(TEMPLATE).render(
        s=status, n=n, experiments=experiments, overview=overview, groups=groups, meta=meta,
        retracted=[r for r in rows if r["status"] == "retracted"])
    settings.site_dir.mkdir(parents=True, exist_ok=True)
    out = settings.site_dir / "index.html"
    out.write_text(html)
    (settings.site_dir / "datasets.csv").write_text((settings.catalog_dir / "datasets.csv").read_text())
    (settings.site_dir / ".nojekyll").touch()
    return out
