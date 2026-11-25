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

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_BLEND = Path(__file__).resolve().parents[2] / "blend" / "scripts" / "blend.py"

# Room left of each forecast map for its slider, and left of each row for its name and
# buttons, in pixels.
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


def _run_blend(out, inputs, *args):
    """blend with --keep-members on inputs, written to out and loaded."""
    import xarray as xr

    if not _BLEND.is_file():
        raise UsageError(f"blend-interface needs the blend skill beside it ({_BLEND})")
    cmd = ["uv", "run", "--quiet", str(_BLEND), "-o", str(out), "--keep-members", *args]
    for i in inputs:
        cmd += ["-i", str(i)]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        raise UsageError(f"blend failed:\n{run.stderr.strip()}")
    for line in run.stderr.splitlines():
        if " missing from " in line:
            print(line, file=sys.stderr)
    return xr.open_zarr(out).load()


def _spec(names, n_rows, absent, title, palette, units, anomaly):
    """Jsonnet: a row per lead of forecast maps, each with a slider strip on its left, then
    the blend. Panel r * cols + k is forecast k in row r; the blend is the row's last panel.

    absent[r] lists the forecasts without row r's lead; their panels are greyed out. With
    anomaly (its palette and units), every panel also holds a hidden anomaly map on
    coloraxis2 with its own colorbar, for the page's Totals / Anomaly toggle.
    """
    n, cols = len(names), len(names) + 1
    panel = max(150, min(210, 1150 // cols))
    j = json.dumps
    anom_palette, anom_units = anomaly or (palette, "")
    # The page paints the blends itself, so their hover shows the place only; the values
    # are in the readout above the maps.
    hover = "%{{y:.2f}}°, %{{x:.2f}}°<extra></extra>"
    return f"""
local ws = import 'ws.libsonnet';
local bbox = std.extVar('inputs').BL.bbox;
local cols = {cols}; local gap = 0.04; local gutter = {_GUTTER} / (cols * {panel});
local anomaly = {j(anomaly is not None)};
local rows = std.range(0, {n_rows - 1}); local models = std.range(0, {n - 1});
local cell(r, k) = r * cols + k;
local ax(c) = if c == 0 then '' else std.toString(c + 1);
local map(name, c, ca, hover, visible=true) = {{type: 'heatmap', name: name, x: '@BL.lon', y: '@BL.lat',
  z: '@BL.' + name, coloraxis: ca, hovertemplate: hover, visible: visible}} + ws.axes(c);
// Anomaly maps go under the base-map lines, next to their totals map.
local layer(name, c, hover, anomHover) = [map(name, c, 'coloraxis', hover)]
  + (if anomaly then [map(name + '_anom', c, 'coloraxis2', anomHover, false)] else []);
local forecast(r, k) = layer('m%d_%d' % [k, r], cell(r, k),
    '%{{z:.1f}} {units}<extra></extra>', '%{{z:.1f}} {anom_units}<extra></extra>')
  + ws.basemap(bbox, cell(r, k), rivers=false, lakes=false, admin=false);
local blend(r) = layer('blend_%d' % r, cell(r, {n}), {j(hover)}, {j(hover)})
  + ws.basemap(bbox, cell(r, {n}), rivers=false, admin=false);
// A grey veil over a forecast's panel in a row whose lead it lacks.
local veil(c) = {{
  type: 'rect', layer: 'above', line: {{width: 0}}, fillcolor: 'rgba(214,218,224,0.85)',
  xref: 'x' + ax(c) + ' domain', x0: 0, x1: 1, yref: 'y' + ax(c) + ' domain', y0: 0, y1: 1,
}};
local smallCells = [cell(r, k) for r in rows for k in models];
{{
  data: std.flattenArrays([forecast(r, k) for r in rows for k in models] + [blend(r) for r in rows]),
  layout: ws.grid(bbox, rows={n_rows}, cols=cols, panel={panel})
    + ws.classBar(ws.palettes.{palette}, {j(units)}, y=0.05, len=0.85)
    + (if anomaly then ws.classBar(ws.palettes.{anom_palette}, {j(anom_units)}, coloraxis='coloraxis2', y=0.05, len=0.85) else {{}})
    + {{
      title: {{text: {j(title)}}},
      // Only the last row's blend has longitude labels; trim the rest of the bottom margin.
      height: super.height - (super.margin.b - 30),
      margin+: {{b: 30, l: {_HEADER}}},
      annotations+: [ws.panelTitle(k, {j(names)}[k]) for k in models] + [ws.panelTitle({n}, '<b>Blend</b>')],
      shapes+: [veil(cell(r, k)) for r in rows for k in {j(absent)}[r]],
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
    + {{['yaxis' + ax(c)]+: {{showticklabels: false, ticks: ''}} for c in std.range(0, {n_rows} * cols - 1)}},
}}
"""


@weather_skill(name="blend-interface", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=_Inputs, action="append", required=True)
@weather_skill.argument("--label", action="append")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--lead", type=int, nargs="+", default=[1])
@weather_skill.argument("--weights")
@weather_skill.argument("--anomaly", type=_Inputs, action="append")
@weather_skill.argument("--palette", default="ppt_week")
def blend_interface(input, label, variable, lead, weights, anomaly, palette, output, **kwargs):
    """Choose blend weights for several forecasts on an interactive HTML page with coupled sliders."""
    if Path(output).suffix.lower() not in (".html", ".htm"):
        raise UsageError(f"--output {output}: the page is written to .html")
    if anomaly and len(anomaly) != len(input):
        raise UsageError(f"pass one --anomaly per --input, in the same order (got {len(anomaly)})")

    import plotly.io as pio
    import xarray as xr
    from weather_skills_plotting.bind import resolve
    from weather_skills_plotting.export import validate
    from weather_skills_plotting.lint import check_bindings
    from weather_skills_plotting.render import SpecArg, evaluate, house_template, input_metadata

    args = ["--lead", *map(str, lead)]
    args += ["--weights", weights] if weights else []
    with tempfile.TemporaryDirectory(prefix="blend-interface-") as work:
        labels = [a for lab in label or [] for a in ("--label", lab)]
        var_arg = ["-v", variable] if variable else []
        ds = _run_blend(Path(work) / "totals.zarr", input, *args, *labels, *var_arg)
        variable = variable or next(v for v in ds.data_vars if not v.endswith("_members"))
        names = [str(m) for m in ds["model"].values]
        views = {"": (ds, variable)}
        if anomaly:
            # Under the forecasts' labels and on their blend's grid, so the views line up.
            if len(names) != len(input):
                raise UsageError("with --anomaly, every forecast must reach one of the leads")
            labels = [a for lab in names for a in ("--label", lab)]
            grid = ["--reference-grid", str(Path(work) / "totals.zarr")]
            anom = _run_blend(Path(work) / "anomaly.zarr", anomaly, *args, *labels, *grid)
            views["_anom"] = (anom, next(v for v in anom.data_vars if not v.endswith("_members")))

    # One 2-D field per panel: m<k>_<row> for forecast k, blend_<row> for the blend.
    lead_dim = ds[variable].dims[0]
    bl = xr.Dataset()
    for suffix, (d, var) in views.items():
        for r in range(len(lead)):
            row = d.isel({lead_dim: r}, drop=True)
            for k in range(len(names)):
                bl[f"m{k}_{r}{suffix}"] = row[f"{var}_members"].isel(model=k, drop=True)
            bl[f"blend_{r}{suffix}"] = row[var]
    bl = bl.astype("float32")  # half the bytes in the page, and plenty for a map

    weights_by_lead = json.loads(ds.attrs["blend_weights"])
    absent = [
        [names.index(m) for m in out] for out in json.loads(ds.attrs["blend_left_out"]).values()
    ]
    word = next(iter(weights_by_lead)).rstrip("0123456789").capitalize()
    is_date = ds[lead_dim].dtype.kind == "M"
    starts = [f"from {str(v)[5:10]}" if is_date else "" for v in ds[lead_dim].values]
    units = str(ds[variable].attrs.get("units", ""))
    init = str(ds.attrs.get("weather_skills_forecast_init", ""))[:10]
    title = f"Forecast blend, init {init}" if init else "Forecast blend"
    anomaly_view = None
    if anomaly:
        anom_units = str(anom[views["_anom"][1]].attrs.get("units", ""))
        anomaly_view = (palette.replace("ppt_", "ppt_anom_", 1), anom_units)

    spec = _spec(names, len(lead), absent, title, palette, units, anomaly_view)
    fig = evaluate(SpecArg(spec), {"BL": input_metadata(bl)}, {})
    fig.setdefault("layout", {}).setdefault("template", house_template())
    check_bindings(fig)
    fig = validate(resolve(fig, {"BL": bl, "const": {}})[0])

    trace = {t.name: k for k, t in enumerate(fig.data) if t.type == "heatmap"}
    cols, rows = len(names) + 1, range(len(lead))
    setup = {
        "labels": names,
        "keys": list(weights_by_lead),
        "weights": list(weights_by_lead.values()),
        "rows": [f"{word} {n}" for n in lead],
        "starts": starts,
        "init": init,
        "present": [[k not in absent[r] for k in range(len(names))] for r in rows],
        # Plotly's axis suffix for each forecast panel: '' for panel 0, else its number + 1.
        "axes": [
            ["" if c == 0 else str(c + 1) for c in range(r * cols, r * cols + len(names))]
            for r in rows
        ],
        "gutter": _GUTTER,
        "header": _HEADER,
        "ascending": bool(bl["latitude"][0] < bl["latitude"][-1]),
    }
    for suffix, key in (("", "totals"), ("_anom", "anomaly")):
        if suffix in views:
            setup[key] = {
                "small": [[trace[f"m{k}_{r}{suffix}"] for k in range(len(names))] for r in rows],
                "blend": [trace[f"blend_{r}{suffix}"] for r in rows],
            }

    page = pio.to_html(
        fig,
        include_plotlyjs=True,
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
.page { height: 100vh; overflow: auto; }
body { margin: 0; background: #fff; color: #1b1f24; font: 13px/1.35 system-ui, -apple-system, "Segoe UI", sans-serif; }
button { font: inherit; padding: 5px 10px; border-radius: 6px; border: 1px solid #c8ced6; background: #fff; cursor: pointer; }
button:hover { border-color: #2563eb; }
button.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
.muted { color: #5b6573; }
.bar { display: flex; gap: 12px; align-items: center; padding: 10px 16px 0; flex-wrap: wrap; }
.steps { padding: 5px 10px; background: #eef4ff; border: 1px solid #c9dafc; border-radius: 6px; }
/* Left of each row: its name and start date, and buttons that reset only that row. */
.rowhead { position: absolute; display: flex; flex-direction: column; gap: 4px; z-index: 5; }
.rowhead b { font-size: 14px; }
.rowhead button { padding: 2px 6px; font-size: 12px; }
/* A vertical 0-1 slider in the strip left of each forecast map, its value on top. */
.wslider { position: absolute; display: flex; flex-direction: column; align-items: center; gap: 2px; z-index: 5; }
.wslider output { color: #2563eb; font: 600 11px/1 system-ui, sans-serif; }
.wslider input { writing-mode: vertical-lr; direction: rtl; flex: 1; width: 18px; margin: 0; accent-color: #2563eb; }
.wslider input:disabled { opacity: 0.3; }
pre { margin: 6px 0 0; padding: 8px 10px; background: #f4f6f8; border: 1px solid #dde2e8; border-radius: 6px;
  font: 12px/1.4 ui-monospace, Menlo, monospace; white-space: pre-wrap; user-select: all; }
#view[hidden] { display: none; }
#view button.on { background: #1b1f24; border-color: #1b1f24; color: #fff; }
.footer { padding: 0 16px 16px; max-width: 1100px; }
</style>"""

_TOOLBAR = """<div class="bar">
<span id="view" hidden><button type="button" data-view="totals" class="on">Totals</button>
<button type="button" data-view="anomaly">Anomaly</button></span>
<span class="muted" id="readout"></span>
</div>"""

# Everything to hand back sits together under the maps.
_FOOTER = """<div class="footer"><div class="bar" style="padding:0">
<button id="download" class="primary" type="button">Download blend</button>
<span class="steps">When satisfied, either download or copy the blend and give it to the agent.</span>
<span id="status"></span></div>
<pre id="weights"></pre></div>"""

# Runs after Plotly draws the figure; {plot_id} is the figure's div. The footer comes
# after the figure in the page, so wait for the whole document.
_SCRIPT = r"""
const start = () => {
const S = __SETUP__;
const gd = document.getElementById('{plot_id}');
const $ = id => document.getElementById(id);
const n = S.labels.length, rowIdx = S.rows.map((_, r) => r);
let view = 'totals';

// Plotly stores z as packed binary ({dtype, bdata, shape}); unpack it to rows.
const TYPES = {f8: Float64Array, f4: Float32Array};
function grid(z) {
  if (Array.isArray(z)) return z;
  const flat = new TYPES[z.dtype](Uint8Array.from(atob(z.bdata), c => c.charCodeAt(0)).buffer);
  const [ny, nx] = String(z.shape).split(',').map(Number);
  return Array.from({length: ny}, (_, j) => Array.from(flat.subarray(j * nx, (j + 1) * nx)));
}
const fields = v => S[v] && S[v].small.map(row => row.map(t => grid(gd.data[t].z)));
const F = {totals: fields('totals'), anomaly: fields('anomaly')};

// A row's weights add to 1 over the forecasts that have its lead; the others stay 0.
function normalize(r, v) {
  v = v.map((x, m) => S.present[r][m] ? x : 0);
  const sum = v.reduce((a, b) => a + b, 0);
  return sum > 0 ? v.map(x => x / sum) : normalize(r, v.map(() => 1));
}
const initial = r => normalize(r, S.labels.map(l => S.weights[r][l] || 0));
let w = rowIdx.map(initial);

// {"week1": {label: weight}, ...}, a lead per line, four decimals adding to 1 (the
// rounding left over goes on the largest). The text under the maps is the downloaded file.
function text() {
  return '{\n' + S.keys.map((key, r) => {
    const q = w[r].map(x => Math.round(x * 10000));
    q[w[r].indexOf(Math.max(...w[r]))] += 10000 - q.reduce((a, b) => a + b, 0);
    return `  "${key}": ` + JSON.stringify(Object.fromEntries(S.labels.map((l, m) => [l, q[m] / 10000])));
  }).join(',\n') + '\n}';
}

// Same rule as the blend skill: blank where any forecast with weight has no value.
function blended(G, v) {
  return G[0].map((row, j) => row.map((_, i) => {
    let s = 0;
    for (let m = 0; m < n; m++) {
      if (v[m] <= 0) continue;
      const x = G[m][j][i];
      if (x == null || !isFinite(x)) return null;
      s += v[m] * x;
    }
    return s;
  }));
}

// Plotly draws each heatmap as an image, and redrawing the figure redraws every base-map
// line too: far too slow to follow a slider. So the blends are painted straight into
// their images, in the colour axis's own colours, and again after anything Plotly redraws.
const pixel = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
function colours(axis) {
  const {cmin, cmax, colorscale} = gd._fullLayout[axis];
  const stops = colorscale.map(([t, c]) => {
    pixel.fillStyle = c; pixel.fillRect(0, 0, 1, 1);
    return [t, [...pixel.getImageData(0, 0, 1, 1).data.slice(0, 3)]];
  });
  return x => {
    const t = Math.min(1, Math.max(0, (x - cmin) / (cmax - cmin)));
    let s = 0;
    while (s < stops.length - 2 && stops[s + 1][0] <= t) s++;
    const [t0, c0] = stops[s], [t1, c1] = stops[s + 1], f = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
    return c0.map((c, i) => c + f * (c1[i] - c));
  };
}
const colour = {totals: colours('coloraxis'), anomaly: F.anomaly && colours('coloraxis2')};

function paint(v, r) {
  const g = [...gd.querySelectorAll('g.hm')].find(g => g.__data__[0].trace.index === S[v].blend[r]);
  const img = g && g.querySelector('image');
  if (!img) return;  // hidden view
  const z = blended(F[v][r], w[r]), ny = z.length, nx = z[0].length;
  const canvas = Object.assign(document.createElement('canvas'), {width: nx, height: ny});
  const ctx = canvas.getContext('2d'), im = ctx.createImageData(nx, ny);
  z.forEach((row, j) => row.forEach((x, i) => {
    if (x != null) im.data.set([...colour[v](x), 255], 4 * ((S.ascending ? ny - 1 - j : j) * nx + i));
  }));
  ctx.putImageData(im, 0, 0);
  img.setAttribute('href', canvas.toDataURL());
}

// Sliders and text follow at once; the rows that changed are repainted once per frame.
const dirty = new Set();
function refresh(...rows) {
  sliders.forEach((row, r) => row.forEach((box, m) => {
    box.querySelector('input').value = w[r][m];
    box.querySelector('output').textContent = S.present[r][m] ? w[r][m].toFixed(2) : '–';
  }));
  $('weights').textContent = text();
  if (!dirty.size) requestAnimationFrame(() => {
    for (const r of dirty) for (const v of ['totals', 'anomaly']) if (F[v]) paint(v, r);
    dirty.clear();
  });
  (rows.length ? rows : rowIdx).forEach(r => dirty.add(r));
}

// Moving a slider rescales the others in its row in proportion so the row still adds to 1.
function setWeight(r, m, v) {
  const rest = 1 - w[r][m], others = S.present[r].filter((p, k) => p && k !== m).length;
  w[r] = normalize(r, w[r].map((x, k) => k === m ? v : rest > 1e-9 ? x * (1 - v) / rest : (1 - v) / others));
  refresh(r);
}

function add(cls, html) {
  const el = Object.assign(document.createElement('div'), {className: cls, innerHTML: html});
  gd.parentElement.appendChild(el);
  return el;
}
gd.parentElement.style.position = 'relative';
const sliders = rowIdx.map(r => S.labels.map((lab, m) => {
  const box = add('wslider', `<output></output><input type="range" min="0" max="1" step="0.01"
    title="${lab}, ${S.rows[r]}" ${S.present[r][m] ? '' : 'disabled'}>`);
  box.querySelector('input').addEventListener('input', e => setWeight(r, m, +e.target.value));
  return box;
}));
const heads = rowIdx.map(r => {
  const head = add('rowhead', `<b>${S.rows[r]}</b><span class="muted">${S.starts[r]}</span>
    <button type="button" title="Every forecast with this lead counts the same">Equal</button>
    <button type="button" title="The weights this page was made with">Initial</button>`);
  const [equal, init] = head.querySelectorAll('button');
  equal.addEventListener('click', () => { w[r] = normalize(r, w[r].map(() => 1)); refresh(r); });
  init.addEventListener('click', () => { w[r] = initial(r); refresh(r); });
  return head;
});

// Each slider fills the strip left of its map; each row's name and buttons sit in the
// margin left of the row.
function place() {
  const L = gd._fullLayout, top = gd.offsetTop, left = gd.offsetLeft;
  rowIdx.forEach(r => {
    const ya = L['yaxis' + S.axes[r][0]];
    Object.assign(heads[r].style, {left: left + 8 + 'px', width: S.header - 16 + 'px',
      top: top + ya._offset + (ya._length - heads[r].offsetHeight) / 2 + 'px'});
    sliders[r].forEach((box, m) => {
      const xa = L['xaxis' + S.axes[r][m]], y = L['yaxis' + S.axes[r][m]];
      Object.assign(box.style, {left: left + xa._offset - S.gutter + 2 + 'px', width: S.gutter - 6 + 'px',
        top: top + y._offset + 'px', height: y._length + 'px'});
    });
  });
}

$('download').addEventListener('click', () => {
  const name = `blend_weights_${S.init || 'init'}_${S.keys.join('_')}.json`;
  const a = Object.assign(document.createElement('a'), {
    href: URL.createObjectURL(new Blob([text()], {type: 'application/json'})), download: name});
  a.click();
  $('status').textContent = `Saved ${name}; if nothing downloaded, copy the text below instead.`;
});

// Totals / Anomaly swaps every map and the colorbar; the weights carry over.
if (F.anomaly) {
  $('view').hidden = false;
  document.querySelectorAll('#view button').forEach(b => b.addEventListener('click', () => {
    view = b.dataset.view;
    const on = v => [...S[v].small.flat(), ...S[v].blend];
    Plotly.restyle(gd, {visible: [...on('totals').map(() => view === 'totals'), ...on('anomaly').map(() => view === 'anomaly')]},
      [...on('totals'), ...on('anomaly')]);
    const show = o => !String(o.name).startsWith('classBar-coloraxis') ? o
      : Object.assign({}, o, {visible: (o.name === 'classBar-coloraxis2') === (view === 'anomaly')});
    Plotly.relayout(gd, {shapes: gd.layout.shapes.map(show), annotations: gd.layout.annotations.map(show)});
    document.querySelectorAll('#view button').forEach(x => x.classList.toggle('on', x === b));
  }));
}

// Hovering a blend shows its value there and what each forecast has.
gd.on('plotly_hover', ev => {
  const p = ev.points[0], r = S[view].blend.indexOf(p.curveNumber);
  if (r < 0) return;
  const [j, i] = p.pointIndex, G = F[view][r];
  const show = x => x == null || !isFinite(x) ? '–' : x.toFixed(1);
  const cell = G.map(g => [[g[j][i]]]);
  $('readout').textContent = `${S.rows[r]}: blend ${show(blended(cell, w[r])[0][0])} · `
    + S.labels.map((l, m) => `${l} ${show(G[m][j][i])}`).join(', ');
});
gd.on('plotly_unhover', () => { $('readout').textContent = ''; });
gd.on('plotly_afterplot', () => { place(); refresh(); });
window.addEventListener('resize', place);
place();
refresh();
};
document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
"""


if __name__ == "__main__":
    blend_interface()
