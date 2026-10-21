"""Turn the member-blend report into the files for a live Dashboard artifact.

Usage: python dashboard_data.py OUT_DIR [RUN_LOG]

OUT_DIR holds report.md and the map_*.png that report.py and run_demo.sh wrote. RUN_LOG, optional,
is run_demo.sh's printed output (stage times). Writes OUT_DIR/dashboard/:
- skill.json, skill_gefs.json, samples.json, weights.json, outlook.json, started.json, run.json, stages.json: the
  datasets, one JSON array of rows each, to upload as files;
- datasets.json: each dataset's title, description and file name, for its datasets/<id> document;
- file_index.html.json and file_map_<name>.js.json: the page (dashboard.html, next to this script)
  and each map as a data: URL, as {"text": ...} documents for files/index.html and files/map_<name>.js.
"""

import base64
import io
import json
import re
import sys
from pathlib import Path

import pandas as pd
from PIL import Image

out = Path(sys.argv[1])
log = Path(sys.argv[2]) if len(sys.argv) > 2 else None
dash = out / "dashboard"
dash.mkdir(exist_ok=True)
report = (out / "report.md").read_text()
sections = {s.split("\n", 1)[0]: s for s in re.split(r"\n## ", report)}
section = lambda start: next(s for title, s in sections.items() if title.startswith(start))  # noqa: E731
pct = lambda s: None if s == "–" else float(s.rstrip("%")) / 100  # noqa: E731  ("–": no forecast)


def blocks(text):
    """(region, its paragraph, its table rows without the header) for each **Region** + table."""
    for m in re.finditer(r"\*\*([^*]+)\*\*([^\n]*)\n\n((?:\|[^\n]*\n?)+)", text):
        rows = [[c.strip() for c in line.strip("|").split("|")] for line in m.group(3).strip().splitlines()]
        yield m.group(1), m.group(2), rows[2:]


skill, samples = [], []
for region, para, rows in blocks(section("How well")):
    n, e7, e14 = re.search(r"([\d,]+) samples, (\d+) / (\d+) onsets", para).groups()
    samples.append({"region": region, "samples": int(n.replace(",", "")), "onsets_7d": int(e7), "onsets_14d": int(e14)})
    skill += [{"region": region, "predictors": r[0], "bss_7d": float(r[1]), "bss_14d": float(r[2])} for r in rows]

# The *With GEFS* table under each region: 7 days only, over the seasons GEFS shares with ECMWF.
skill_gefs = []
how = section("How well")
for m in re.finditer(r"\*With GEFS\*, within 7 days only, (\d+) seasons \(([^)]*)\): ([\d,]+) samples, (\d+) onsets\.\n\n((?:\|[^\n]*\n?)+)", how):
    region = re.findall(r"\*\*([^*]+)\*\*", how[:m.start()])[-1]
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in m.group(5).strip().splitlines()][2:]
    skill_gefs += [{"region": region, "predictors": r[0], "bss_7d": float(r[1]), "seasons": m.group(2),
                    "samples": int(m.group(3).replace(",", "")), "onsets_7d": int(m.group(4))} for r in rows]

weights = []
for line in section("Blend weights").splitlines():
    c = [x.strip() for x in line.strip("|").split("|")]
    if len(c) == 4 and c[1].endswith(" days"):
        coef = {k: float(v) for k, v in re.findall(r"(ECMWF|Cumulus|GEFS) ([-\d.]+)", c[3])}
        weights.append({"blend": c[0], "window_days": int(c[1].split()[0]), "intercept": float(c[2]),
                        "w_ecmwf": coef.get("ECMWF"), "w_cumulus": coef.get("Cumulus"), "w_gefs": coef.get("GEFS")})

outlook, started = [], []
cells = {region: int(n) for region, n in re.findall(r"\*\*(\w+)\*\* \((\d+) cells\)", section("How well"))}
for region, para, rows in blocks(section("2026 outlook")):
    started.append({"region": region, "cells_started": int(re.search(r"in (\d+) cells", para).group(1)),
                    "cells_total": cells.get(region)})
    outlook += [{"region": region, "forecast": r[0], "p_7d": pct(r[1]), "p_14d": pct(r[2])} for r in rows]

init = re.search(r"init (\d{4}-\d{2}-\d{2})", report).group(1)
by = [f"{pd.Timestamp(init) + pd.Timedelta(days=w - 1):%-d %b}" for w in (7, 14)]
run = [{"init": init, "init_label": f"{pd.Timestamp(init):%-d %B %Y}", "by_7d": by[0], "by_14d": by[1],
        "made": f"{pd.Timestamp((out / 'report.md').stat().st_mtime, unit='s'):%-d %B %Y}"}]
stages = []
if log:
    stages = [{"stage": m.group(1), "seconds": int(m.group(2))}
              for m in re.finditer(r"^(\S.*?)\s+(\d+)s  \(total", log.read_text(), re.M)]

source = f"From report.md of run_demo.sh (init {init}). "
DATASETS = {
    "run": ("Forecast date and windows", "The live init and the last day of each window.", run),
    "skill": ("Cross-validated blend skill", "Brier skill score against climatology, leave-one-season-out, "
              "by region and predictor set, within 7 and 14 days.", skill),
    "skill_gefs": ("Blend skill with GEFS", "Brier skill score against climatology, leave-one-season-out, "
                   "within 7 days, over the seasons the GEFS reforecast shares with ECMWF.", skill_gefs),
    "samples": ("Cross-validation sample counts", "Samples (init x cell) and observed onsets within "
                "7 / 14 days scored per region.", samples),
    "weights": ("Fitted blend weights", "Logistic-regression intercept and coefficients (log-odds per unit "
                "of the members' onset share), fitted on all seasons.", weights),
    "outlook": ("Onset outlook", "Area-mean chance the rains have started within 7 / 14 days, per region "
                "and forecast; cells where CHIRPS already shows the onset count as 1.", outlook),
    "started": ("Cells already started", "1.5 deg cells where CHIRPS shows the onset before the init, "
                "and cells per country.", started),
    "stages": ("Demo stage timings", "Seconds per stage, as run_demo.sh printed them.", stages),
}
manifest = {}
for key, (title, description, rows) in DATASETS.items():
    if not rows:
        continue
    (dash / f"{key}.json").write_text(json.dumps(rows, indent=1))
    desc = description if key == "stages" else source + description
    manifest[key] = {"title": title, "description": desc, "name": f"{key}.json"}
(dash / "datasets.json").write_text(json.dumps(manifest, indent=1))

page = (Path(__file__).parent / "dashboard.html").read_text()
(dash / "file_index.html.json").write_text(json.dumps({"text": page}))
MAPS = {"all": "map_ecmwf+cumulus", "kenya": "map_ecmwf+cumulus_Kenya", "uganda": "map_ecmwf+cumulus_Uganda",
        "cumulus": "map_cumulus", "ecmwf": "map_ecmwf"}
for key, png in MAPS.items():
    buf = io.BytesIO()
    Image.open(out / f"{png}.png").convert("RGB").save(buf, "WEBP", quality=82)  # a page file holds at most 256 KB
    uri = "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()
    js = f"window.BLEND_MAPS = window.BLEND_MAPS || {{}};\nwindow.BLEND_MAPS[{json.dumps(key)}] = {json.dumps(uri)};\n"
    (dash / f"file_map_{key}.js.json").write_text(json.dumps({"text": js}))
print(dash)
