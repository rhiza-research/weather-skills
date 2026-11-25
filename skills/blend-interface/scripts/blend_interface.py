# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.4.0",
#   "plotly",
#   "weather-skills-plotting @ git+https://github.com/weather-skills/weather-skills-plotting@main",
# ]
# ///
"""Choose blend weights for several forecasts on an interactive HTML page with coupled sliders."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.standard_utils import grid_spacing

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_BLEND = Path(__file__).resolve().parents[2] / "blend" / "scripts" / "blend.py"

# Room left of each forecast map for its vertical weight slider, and left of each row
# for its name and buttons, in pixels.
_GUTTER = 36
_HEADER = 96


class _Inputs:
    """The forecast Zarrs, passed on to blend; recorded in this page's provenance."""

    def __init__(self, path):
        self.path = path

    def zarr_paths(self):
        return [Path(self.path)]

    def __str__(self):
        return str(self.path)


def _default_label(path):
    """blend's default --label: windborne_wm6_weekly_totals_init2026-11-02.zarr -> windborne_wm6"""
    name = Path(str(path).rstrip("/")).name.removesuffix(".zarr")
    for marker in ("_weekly", "_daily", "_monthly", "_init"):
        name = name.split(marker)[0]
    return name or str(path)


def _run_blend(inputs, label, variable, leads, weights, reference_grid, out):
    if not _BLEND.is_file():
        raise UsageError(f"blend-interface needs the blend skill beside it ({_BLEND})")
    cmd = ["uv", "run", "--quiet", str(_BLEND), "-o", str(out), "--keep-members"]
    for lead in leads:
        cmd += ["--lead", str(lead)]
    for i in inputs:
        cmd += ["-i", str(i)]
    for lab in label or []:
        cmd += ["--label", lab]
    for flag, value in (
        ("--variable", variable),
        ("--weights", weights),
        ("--reference-grid", reference_grid),
    ):
        if value:
            cmd += [flag, value]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        raise UsageError(f"blend failed:\n{run.stderr.strip()}")
    for line in run.stderr.splitlines():
        if line.startswith("blend: ") and " missing from " in line:
            print(line, file=sys.stderr)


def _layout(n_models, n_leads):
    """One row per lead: the forecasts' maps, then the blend; panel numbers row by row."""
    cols = n_models + 1
    small = [[r * cols + k for k in range(n_models)] for r in range(n_leads)]
    blend = [r * cols + n_models for r in range(n_leads)]
    return cols, small, blend


def _spec(names, row_labels, absent, title, subtitle, palette, units, anomaly):
    """Jsonnet: a row per lead of small maps with a slider strip left of each, the blend last.

    absent[r] lists the models without row r's lead; their panels are greyed out. With anomaly
    (its palette and units), every panel also holds a hidden anomaly map on coloraxis2
    with its own colorbar, for the page's Totals / Anomaly toggle.
    """
    n_models, n_leads = len(names), len(row_labels)
    cols, small, blend = _layout(n_models, n_leads)
    panel = max(150, min(210, 1150 // cols))
    j = json.dumps
    anom_palette, anom_units = anomaly or (None, "")

    def hover(u):
        return "%{y:.2f}°, %{x:.2f}°<br><b>blend %{z:.1f} " + u + "</b><extra></extra>"

    return f"""
local ws = import 'ws.libsonnet';
local bbox = std.extVar('inputs').BL.bbox;
local cols = {cols}; local small = {j(small)}; local blend = {j(blend)};
local smallCells = std.flattenArrays(small); local gap = 0.04;
local gutter = {_GUTTER} / (cols * {panel}); local anomaly = {j(anomaly is not None)};
local ax(c) = if c == 0 then '' else std.toString(c + 1);
local map(name, c, ca, hover, visible=true) = {{type: 'heatmap', name: name, x: '@BL.lon', y: '@BL.lat',
  z: '@BL.' + name, coloraxis: ca, hovertemplate: hover, visible: visible}} + ws.axes(c);
// Anomaly maps go under the base-map lines, next to their totals map.
local layer(name, c, hover, anomHover) = [map(name, c, 'coloraxis', hover)]
  + (if anomaly then [map(name + '_anom', c, 'coloraxis2', anomHover, false)] else []);
local smallMap(r, k) = layer('m%d_%d' % [k, r], small[r][k],
    '%{{z:.1f}} {units}<extra></extra>', '%{{z:.1f}} {anom_units}<extra></extra>')
  + ws.basemap(bbox, small[r][k], rivers=false, lakes=false, admin=false);
local blendMap(r) = layer('blend_%d' % r, blend[r], {j(hover(units))}, {j(hover(anom_units))})
  + ws.basemap(bbox, blend[r], rivers=false, admin=1);
// A grey veil over a forecast's panel in a row whose lead it lacks.
local veil(c) = {{
  type: 'rect', name: 'missing-lead', layer: 'above', line: {{width: 0}}, fillcolor: 'rgba(214,218,224,0.85)',
  xref: 'x' + ax(c) + ' domain', x0: 0, x1: 1, yref: 'y' + ax(c) + ' domain', y0: 0, y1: 1,
}};
{{
  data: std.flattenArrays([smallMap(r, k) for r in std.range(0, {n_leads - 1}) for k in std.range(0, {n_models - 1})]
    + [blendMap(r) for r in std.range(0, {n_leads - 1})]),
  layout: ws.grid(bbox, rows={n_leads}, cols=cols, panel={panel})
    + ws.classBar(ws.palettes.{palette}, {j(units)}, y=0.05, len=0.85)
    + (if anomaly then ws.classBar(ws.palettes.{anom_palette or palette}, {j(anom_units)}, coloraxis='coloraxis2', y=0.05, len=0.85) else {{}})
    + {{
      title: {{text: {j(title)}, subtitle: {{text: {j(subtitle)}}}}},
      // Only the blends on the last row have longitude labels, so most of ws.grid's
      // bottom margin is empty; trim it (and the height with it).
      height: super.height - (super.margin.b - 30),
      margin+: {{b: 30, l: {_HEADER}}},
      annotations+: [ws.panelTitle(small[0][k], {j(names)}[k]) for k in std.range(0, {n_models - 1})]
        + [ws.panelTitle(blend[0], '<b>Blend</b>')],
      shapes+: [veil(small[r][k]) for r in std.range(0, {n_leads - 1}) for k in {j(absent)}[r]],
    }}
    // The anomaly colorbar starts hidden; the toggle swaps it with the totals one.
    + {{
      local hide(o) = if std.get(o, 'name', '') == 'classBar-coloraxis2' then o + {{visible: false}} else o,
      shapes: [hide(o) for o in super.shapes],
      annotations: [hide(o) for o in super.annotations],
    }}
    // Each forecast map gives up a strip on its left to its slider.
    + {{['xaxis' + ax(c)]+: {{domain: [(c % cols) / cols + gutter, (c % cols + 1) / cols - gap / 2],
                             showticklabels: false, ticks: ''}} for c in smallCells}}
    + {{['yaxis' + ax(c)]+: {{showticklabels: false, ticks: ''}} for c in smallCells + blend}},
}}
"""


def _figure(bl, *spec_args):
    from weather_skills_plotting.bind import resolve
    from weather_skills_plotting.export import validate
    from weather_skills_plotting.lint import check_bindings
    from weather_skills_plotting.render import SpecArg, evaluate, house_template, input_metadata

    figure = evaluate(SpecArg(_spec(*spec_args)), {"BL": input_metadata(bl)}, {})
    figure.setdefault("layout", {}).setdefault("template", house_template())
    check_bindings(figure)
    resolved, _ = resolve(figure, {"BL": bl, "const": {}})
    return validate(resolved)


def _per_lead(da, lead_dim):
    """da split along its lead axis (or [da] when it has none), each squeezed to lat/lon."""
    if lead_dim is None:
        return [da.squeeze(drop=True)]
    return [da.isel({lead_dim: r}).squeeze(drop=True) for r in range(da.sizes[lead_dim])]


@weather_skill(name="blend-interface", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=_Inputs, action="append", required=True)
@weather_skill.argument("--label", action="append")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--lead", action="append")
@weather_skill.argument("--weights")
@weather_skill.argument("--reference-grid")
@weather_skill.argument("--anomaly", type=_Inputs, action="append")
@weather_skill.argument("--palette", default="ppt_week")
@weather_skill.argument("--anomaly-palette", default="ppt_anom_week")
@weather_skill.argument("--title")
def blend_interface(
    input,
    label,
    variable,
    lead,
    weights,
    reference_grid,
    anomaly,
    palette,
    anomaly_palette,
    title,
    output,
    **kwargs,
):
    """Choose blend weights for several forecasts on an interactive HTML page with coupled sliders."""
    if Path(output).suffix.lower() not in (".html", ".htm"):
        raise UsageError(f"--output {output}: the page is written to .html")
    inputs = [str(i) for i in input]
    anomalies = [str(a) for a in anomaly or []]
    if anomalies and len(anomalies) != len(inputs):
        raise UsageError(
            f"got {len(anomalies)} --anomaly for {len(inputs)} --input; pass one per input, in the same order"
        )
    lead = lead or ["1"]

    import plotly.io as pio
    import xarray as xr

    # Both runs use the same labels, so the anomaly maps pair with their forecasts.
    labels = label or ([_default_label(p) for p in inputs] if anomalies else None)
    with tempfile.TemporaryDirectory(prefix="blend-interface-") as work:
        zarr = Path(work) / "members.zarr"
        _run_blend(inputs, labels, variable, lead, weights, reference_grid, zarr)
        ds = xr.open_zarr(zarr).load()
        if anomalies:
            anom_zarr = Path(work) / "anomaly.zarr"
            # On the totals blend's grid, so the two views line up cell for cell.
            _run_blend(anomalies, labels, None, lead, weights, str(zarr), anom_zarr)
            anom = xr.open_zarr(anom_zarr).load()

    variable = next(v for v in ds.data_vars if not v.endswith("_members"))
    members = ds[f"{variable}_members"]
    names = [str(m) for m in members["model"].values]
    leads = json.loads(ds.attrs["blend_leads"])
    lead_dim = next((d for d in ds[variable].dims if d not in ("latitude", "longitude")), None)
    units = str(ds[variable].attrs.get("units", ""))
    left_out = json.loads(ds.attrs.get("blend_left_out", "{}"))
    # blend keys these by lead (week1, week2, ...), in lead order.
    absent = [[names.index(m) for m in out if m in names] for out in left_out.values()]

    bl = xr.Dataset()
    for r, (field, blended) in enumerate(
        zip(_per_lead(members, lead_dim), _per_lead(ds[variable], lead_dim), strict=True)
    ):
        for k in range(len(names)):
            bl[f"m{k}_{r}"] = field.isel(model=k, drop=True)
        bl[f"blend_{r}"] = blended
    anomaly_view = None
    if anomalies:
        avar = next(v for v in anom.data_vars if not v.endswith("_members"))
        amembers = anom[f"{avar}_members"]
        if [str(m) for m in amembers["model"].values] != names:
            raise UsageError(
                f"the anomalies reach the leads for {list(amembers['model'].values)} but the "
                f"forecasts for {names}; pass an anomaly Zarr for each forecast, in -i order"
            )
        for r, (field, blended) in enumerate(
            zip(_per_lead(amembers, lead_dim), _per_lead(anom[avar], lead_dim), strict=True)
        ):
            for k in range(len(names)):
                bl[f"m{k}_{r}_anom"] = field.isel(model=k, drop=True)
            bl[f"blend_{r}_anom"] = blended
        anomaly_view = (anomaly_palette, str(anom[avar].attrs.get("units", "")))

    init = str(ds.attrs.get("weather_skills_forecast_init", ""))[:10]
    starts = (
        [str(v)[:10] for v in ds[lead_dim].values]
        if lead_dim is not None and ds[lead_dim].dtype.kind == "M"
        else [""] * len(leads)
    )
    period = ds[variable].attrs.get("aggregation_period", "")
    word = "Week" if period == "7 day" else "Lead"
    row_labels = [
        f"{word} {n}" + (f" · {s[5:]}" if s else "") for n, s in zip(leads, starts, strict=True)
    ]
    span = (
        f"{word.lower()} {leads[0]}"
        if len(leads) == 1
        else f"{word.lower()}s {leads[0]}–{leads[-1]}"
    )
    title = title or f"Forecast blend, {span}" + (f" ({period} totals)" if period else "")
    subtitle = " · ".join(
        s
        for s in (
            f"init {init}" if init else "",
            f"{len(names)} forecasts on a {grid_spacing(bl['latitude'].values):g}° grid",
        )
        if s
    )

    fig = _figure(bl, names, row_labels, absent, title, subtitle, palette, units, anomaly_view)
    trace = {t.name: k for k, t in enumerate(fig.data) if t.type == "heatmap"}
    _, small_cells, _ = _layout(len(names), len(leads))
    rows = range(len(leads))
    weight_attr = json.loads(ds.attrs["blend_weights"])
    setup = {
        "labels": names,
        "leads": leads,
        "rowLabels": row_labels,
        "rowNames": [f"{word} {n}" for n in leads],
        "rowStarts": [s[5:] for s in starts],
        "present": [[k not in absent[r] for k in range(len(names))] for r in rows],
        "small": [[trace[f"m{k}_{r}"] for k in range(len(names))] for r in rows],
        "blend": [trace[f"blend_{r}"] for r in rows],
        "units": units,
        "anomaly": anomaly_view
        and {
            "small": [[trace[f"m{k}_{r}_anom"] for k in range(len(names))] for r in rows],
            "blend": [trace[f"blend_{r}_anom"] for r in rows],
            "units": anomaly_view[1],
        },
        "axes": [["" if c == 0 else str(c + 1) for c in row] for row in small_cells],
        "gutter": _GUTTER,
        "header": _HEADER,
        # One weights dictionary per row, whatever blend wrote.
        "weights": list(weight_attr.values()),
        "keys": list(weight_attr),
        "record": {
            "skill": "blend",
            "inputs": inputs,
            **({"labels": label} if label else {}),
            "variable": variable,
            "leads": leads,
            "init": init,
            "starts": starts,
        },
    }
    page = pio.to_html(
        fig,
        include_plotlyjs=True,
        full_html=True,
        div_id="blendfig",
        post_script=_SCRIPT.replace("__SETUP__", json.dumps(setup)),
        # Without scroll zoom the wheel scrolls the page past the rows of maps.
        config={"scrollZoom": False, "showSendToCloud": False, "displaylogo": False},
    )
    page = page.replace("<head>", "<head>\n<title>Forecast Blend</title>\n" + _CSS, 1)
    page = page.replace("<body>", '<body>\n<div class="page">\n' + _TOOLBAR, 1)
    page = page.replace("</body>", _FOOTER + "\n</div>\n</body>", 1)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


_CSS = """<style>
/* The page scrolls in its own box, so it scrolls in viewers that show it at a fixed
   height without scrolling the frame. */
html, body { height: 100%; overflow: hidden; }
.page { height: 100vh; overflow-x: auto; overflow-y: scroll; }
body { margin: 0; background: #fff; color: #1b1f24; font: 13px/1.35 system-ui, -apple-system, "Segoe UI", sans-serif; }
button { font: inherit; padding: 5px 10px; border-radius: 6px; border: 1px solid #c8ced6; background: #fff; cursor: pointer; }
button:hover { border-color: #2563eb; }
button.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
/* Left of each row: its name and start date, and buttons that reset only that row. */
.rowhead { position: absolute; display: flex; flex-direction: column; gap: 4px; z-index: 5; }
.rowhead b { font-size: 14px; }
.rowhead button { padding: 2px 6px; font-size: 12px; }
.muted { color: #5b6573; font-variant-numeric: tabular-nums; }
.steps { padding: 5px 10px; background: #eef4ff; border: 1px solid #c9dafc; border-radius: 6px; }
.bar { display: flex; gap: 12px; align-items: center; padding: 10px 16px 0; flex-wrap: wrap; }
/* A vertical 0-1 slider in the strip left of each forecast map, its value on top. */
.wslider { position: absolute; display: flex; flex-direction: column; align-items: center; gap: 2px; z-index: 5; }
.wslider output { color: #2563eb; font: 600 11px/1 system-ui, sans-serif; font-variant-numeric: tabular-nums; }
.wslider input { writing-mode: vertical-lr; direction: rtl; flex: 1; width: 18px; margin: 0; accent-color: #2563eb; }
.wslider input:disabled { opacity: 0.3; }
pre { margin: 6px 0 0; padding: 8px 10px; background: #f4f6f8; border: 1px solid #dde2e8; border-radius: 6px;
  font: 12px/1.4 ui-monospace, Menlo, monospace; white-space: pre-wrap; word-break: break-all; user-select: all; }
.seg { display: inline-flex; }
.seg[hidden] { display: none; }
.seg button { border-radius: 0; margin-left: -1px; }
.seg button:first-child { border-radius: 6px 0 0 6px; margin-left: 0; }
.seg button:last-child { border-radius: 0 6px 6px 0; }
.seg button.on { background: #1b1f24; border-color: #1b1f24; color: #fff; }
.footer { padding: 0 16px 16px; max-width: 1100px; }
</style>"""

_TOOLBAR = """<div class="bar">
<span class="seg" id="view" hidden><button type="button" data-view="totals" class="on">Totals</button><button type="button" data-view="anomaly">Anomaly</button></span>
<span class="muted" id="readout"></span>
</div>"""

# Everything to hand back sits together under the maps.
_FOOTER = """<div class="footer"><div class="bar" style="padding:0">
<button id="download" class="primary" type="button">Download blend</button>
<span class="steps">When satisfied, either download or copy the blend and give it to the agent.</span>
<span id="status"></span></div>
<pre id="weights"></pre></div>"""

# Runs after Plotly draws the figure; {plot_id} is the figure's div. The footer
# comes after the figure in the page, so wait for the whole document.
_SCRIPT = r"""
const start = () => {
const S = __SETUP__;
const gd = document.getElementById('{plot_id}');
const n = S.labels.length, L = S.leads.length;
const $ = id => document.getElementById(id);
const rowsIdx = [...Array(L).keys()];

// Plotly stores z as packed binary ({dtype, bdata, shape}); unpack it to rows.
const TYPES = {f8: Float64Array, f4: Float32Array, i4: Int32Array, i2: Int16Array, i1: Int8Array, u1: Uint8Array};
function rows(z) {
  if (Array.isArray(z)) return z;
  const bytes = Uint8Array.from(atob(z.bdata), c => c.charCodeAt(0));
  const flat = new TYPES[z.dtype](bytes.buffer);
  const [ny, nx] = String(z.shape).split(',').map(Number);
  return Array.from({length: ny}, (_, j) => Array.from(flat.subarray(j * nx, (j + 1) * nx)));
}
const Z = S.small.map(row => row.map(k => rows(gd.data[k].z)));
const A = S.anomaly && S.anomaly.small.map(row => row.map(k => rows(gd.data[k].z)));
let view = 'totals';

// Weights sum to 1 over the forecasts that have the row's lead; the others stay 0.
function normalize(r, v) {
  v = v.map((x, m) => S.present[r][m] && x > 0 ? x : 0);
  const sum = v.reduce((a, b) => a + b, 0);
  const k = S.present[r].filter(Boolean).length;
  return sum > 0 ? v.map(x => x / sum) : v.map((_, m) => S.present[r][m] ? 1 / k : 0);
}
const equal = r => normalize(r, new Array(n).fill(1));

// Weights start from the address (#w=<the weights line>), so a reload or a shared
// link keeps them, else from the weights the page was built with.
function fromHash() {
  try {
    const m = /[#&]w=([^&]*)/.exec(location.hash);
    if (!m) return null;
    const got = JSON.parse(decodeURIComponent(m[1]));
    const perRow = S.keys.map(k => got[k]);
    return perRow.map((g, r) => normalize(r, S.labels.map(l => parseFloat(g[l]) || 0)));
  } catch (e) { return null; }
}
// What each row's Initial button goes back to: the weights the page was made with.
const initial = r => normalize(r, S.labels.map(l => S.weights[r][l] || 0));
let w = fromHash() || rowsIdx.map(r => initial(r));

// Four decimals, with the rounding left over put on the largest so they add to exactly 1.
function weightMap(v) {
  const r = v.map(x => Math.round(x * 10000));
  r[v.indexOf(Math.max(...v))] += 10000 - r.reduce((a, b) => a + b, 0);
  return Object.fromEntries(S.labels.map((l, m) => [l, r[m] / 10000]));
}
// {"week1": {label: weight}, "week2": ...}, keyed as blend keys them, a lead per line.
// The text under the maps and the downloaded file are this same string.
const record = () => Object.fromEntries(S.keys.map((k, r) => [k, weightMap(w[r])]));
const recordText = () =>
  '{\n' + S.keys.map((k, r) => `  "${k}": ${JSON.stringify(weightMap(w[r]))}`).join(',\n') + '\n}';

gd.parentElement.style.position = 'relative';
const boxes = rowsIdx.map(r => S.labels.map((lab, m) => {
  const box = document.createElement('div');
  box.className = 'wslider';
  box.innerHTML = `<output></output><input type="range" min="0" max="1" step="0.01"
    aria-label="Weight for ${lab}, ${S.rowLabels[r]}">`;
  const input = box.querySelector('input');
  input.disabled = !S.present[r][m];
  input.title = S.present[r][m] ? `${lab}, ${S.rowLabels[r]}` : `${lab} has no ${S.rowLabels[r]}`;
  input.addEventListener('input', e => setWeight(r, m, parseFloat(e.target.value)));
  gd.parentElement.appendChild(box);
  return box;
}));

const heads = rowsIdx.map(r => {
  const head = document.createElement('div');
  head.className = 'rowhead';
  head.innerHTML = `<b>${S.rowNames[r]}</b>` + (S.rowStarts[r] ? `<span class="muted">from ${S.rowStarts[r]}</span>` : '')
    + `<button type="button" title="Every forecast with this lead counts the same">Equal</button>`
    + `<button type="button" title="The weights this page was made with">Initial</button>`;
  const [eq, init] = head.querySelectorAll('button');
  eq.addEventListener('click', () => { w[r] = equal(r); refresh(); });
  init.addEventListener('click', () => { w[r] = initial(r); refresh(); });
  gd.parentElement.appendChild(head);
  return head;
});

// Each slider fills the strip left of its map, as tall as the map; each row's name and
// buttons sit in the margin left of the row, centred on it.
function place() {
  const FL = gd._fullLayout;
  heads.forEach((head, r) => {
    const ya = FL['yaxis' + S.axes[r][0]];
    head.style.left = (gd.offsetLeft + 8) + 'px';
    head.style.width = (S.header - 16) + 'px';
    head.style.top = (gd.offsetTop + ya._offset + (ya._length - head.offsetHeight) / 2) + 'px';
  });
  boxes.forEach((row, r) => row.forEach((box, m) => {
    const xa = FL['xaxis' + S.axes[r][m]], ya = FL['yaxis' + S.axes[r][m]];
    box.style.left = (gd.offsetLeft + xa._offset - S.gutter + 2) + 'px';
    box.style.width = (S.gutter - 6) + 'px';
    box.style.top = (gd.offsetTop + ya._offset) + 'px';
    box.style.height = ya._length + 'px';
  }));
}

// Moving one slider rescales the others in its row in proportion so the row still adds
// to 1; other rows keep their own weights.
function setWeight(r, m, v) {
  v = Math.max(0, Math.min(1, v));
  const live = S.present[r].filter(Boolean).length;
  const rest = w[r].reduce((a, x, k) => k === m ? a : a + x, 0);
  w[r] = w[r].map((x, k) => k === m ? v : !S.present[r][k] ? 0
    : rest > 1e-9 ? x * (1 - v) / rest : (1 - v) / Math.max(1, live - 1));
  refresh();
}

// Same rule as the blend skill: blank where any forecast with weight has no value.
function blended(F, v) {
  return F[0].map((row, j) => row.map((_, i) => {
    let s = 0;
    for (let m = 0; m < n; m++) {
      if (v[m] <= 0) continue;
      const x = F[m][j][i];
      if (x == null || !isFinite(x)) return null;
      s += v[m] * x;
    }
    return s;
  }));
}

function refresh() {
  boxes.forEach((row, r) => row.forEach((box, m) => {
    box.querySelector('input').value = w[r][m].toFixed(2);
    box.querySelector('output').textContent = S.present[r][m] ? w[r][m].toFixed(2) : '–';
  }));
  $('weights').textContent = recordText();
  const z = rowsIdx.map(r => blended(Z[r], w[r])), traces = [...S.blend];
  if (A) { z.push(...rowsIdx.map(r => blended(A[r], w[r]))); traces.push(...S.anomaly.blend); }
  Plotly.restyle(gd, {z: z}, traces);
  try { history.replaceState(null, '', '#w=' + encodeURIComponent(JSON.stringify(record()))); } catch (e) {}
}

// Download blend saves the weights as a file to upload to the chat, and also posts the
// record to the page that embeds this one, for hosts that listen.
$('download').addEventListener('click', () => {
  const text = recordText(), r = Object.assign({}, S.record, {weights: record()});
  const span = L === 1 ? S.keys[0] : S.keys[0].replace(/\d+$/, 's') + S.leads.join('-');
  const name = `blend_weights_${r.init || 'init'}_${span}.json`;
  try {
    const url = URL.createObjectURL(new Blob([text], {type: 'application/json'}));
    const a = Object.assign(document.createElement('a'), {href: url, download: name});
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $('status').textContent = `Downloaded ${name}: upload it to your chat.`;
  } catch (e) {
    $('status').textContent = 'The download was blocked; copy the text below the maps into your chat instead.';
  }
  try { if (window.parent !== window) window.parent.postMessage({type: 'weather-skills-blend', record: r}, '*'); } catch (e) {}
});

// Totals / Anomaly swaps every map and the colorbar; the weights carry over.
function setView(v) {
  view = v;
  const anom = v === 'anomaly';
  const totals = [...S.small.flat(), ...S.blend], anoms = [...S.anomaly.small.flat(), ...S.anomaly.blend];
  Plotly.restyle(gd, {visible: [...totals.map(() => !anom), ...anoms.map(() => anom)]}, [...totals, ...anoms]);
  const show = o => {
    if (o.name === 'classBar-coloraxis') return Object.assign({}, o, {visible: !anom});
    if (o.name === 'classBar-coloraxis2') return Object.assign({}, o, {visible: anom});
    return o;
  };
  Plotly.relayout(gd, {shapes: gd.layout.shapes.map(show), annotations: gd.layout.annotations.map(show)});
  document.querySelectorAll('#view button').forEach(b => b.classList.toggle('on', b.dataset.view === v));
}
if (A) {
  $('view').hidden = false;
  document.querySelectorAll('#view button').forEach(b => b.addEventListener('click', () => setView(b.dataset.view)));
}

// Hovering a blend lists what each forecast has in that cell.
gd.on('plotly_hover', ev => {
  const p = ev.points[0], anom = view === 'anomaly';
  const r = (anom ? S.anomaly.blend : S.blend).indexOf(p.curveNumber);
  if (r < 0) return;
  const [j, i] = p.pointIndex, F = (anom ? A : Z)[r];
  $('readout').textContent = `${S.rowLabels[r]}: ` + S.labels.map((l, m) => {
    const v = F[m][j][i];
    return `${l} ${v == null || !isFinite(v) ? '–' : v.toFixed(1)}`;
  }).join(', ');
});
gd.on('plotly_unhover', () => { $('readout').textContent = ''; });
gd.on('plotly_afterplot', place);
window.addEventListener('resize', place);
place();
refresh();
};
document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
"""


if __name__ == "__main__":
    blend_interface()
