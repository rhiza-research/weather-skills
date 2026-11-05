# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.4",
#   "weather-skills-plotting @ git+https://github.com/rhiza-research/weather-skills-plotting@main",
# ]
# ///
"""Choose blend weights for several forecasts on an interactive HTML page with coupled sliders."""

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from weather_skills_core import UsageError, weather_skill
from weather_skills_core.standard_utils import grid_spacing

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

_BLEND = Path(__file__).resolve().parents[2] / "blend" / "scripts" / "blend.py"


class _Inputs:
    """The forecast Zarrs, passed on to blend; recorded in this page's provenance."""

    def __init__(self, path):
        self.path = path

    def zarr_paths(self):
        return [Path(self.path)]

    def __str__(self):
        return str(self.path)


def _run_blend(inputs, label, variable, lead, weights, reference_grid, out):
    if not _BLEND.is_file():
        raise UsageError(f"blend-interface needs the blend skill beside it ({_BLEND})")
    cmd = ["uv", "run", "--quiet", str(_BLEND), "-o", str(out), "--lead", str(lead), "--keep-members"]
    for i in inputs:
        cmd += ["-i", str(i)]
    for lab in label or []:
        cmd += ["--label", lab]
    for flag, value in (("--variable", variable), ("--weights", weights), ("--reference-grid", reference_grid)):
        if value:
            cmd += [flag, value]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        raise UsageError(f"blend failed:\n{run.stderr.strip()}")
    for line in run.stderr.splitlines():
        if line.startswith("blend: lead") and "missing" in line:
            print(line, file=sys.stderr)


def _layout(n):
    """Grid cells for n small maps (left, in up to two rows) and the blend (right, spanning them)."""
    rows = 1 if n <= 3 else 2
    small_cols = math.ceil(n / rows)
    cols = small_cols + rows  # the blend is as many panels wide as it is tall
    cells = [(k // small_cols) * cols + k % small_cols for k in range(n)]
    blend = small_cols
    empty = [c for c in range(rows * cols) if c not in cells and c != blend]
    return rows, cols, small_cols, cells, blend, empty


def _spec(n, title, subtitle, palette, units):
    """Jsonnet: a small map per forecast, the blend large on the right, one class colorbar."""
    rows, cols, small_cols, cells, b, empty = _layout(n)
    j = json.dumps
    hover = "%{y:.2f}°, %{x:.2f}°<br><b>blend %{z:.1f} " + units + "</b><extra></extra>"
    return f"""
local ws = import 'ws.libsonnet';
local bbox = std.extVar('inputs').BL.bbox;
local cells = {j(cells)}; local b = {b}; local gap = 0.04;
local ax(c) = if c == 0 then '' else std.toString(c + 1);
local small(k) = [{{type: 'heatmap', x: '@BL.lon', y: '@BL.lat', z: '@BL.m%d' % k,
  coloraxis: 'coloraxis', hovertemplate: '%{{z:.1f}} {units}<extra></extra>'}} + ws.axes(cells[k])]
  + ws.basemap(bbox, cells[k], rivers=false, lakes=false, admin=false);
{{
  data: std.flattenArrays([small(k) for k in std.range(0, std.length(cells) - 1)])
    + [{{type: 'heatmap', x: '@BL.lon', y: '@BL.lat', z: '@BL.blend', name: 'blend',
        coloraxis: 'coloraxis', hovertemplate: {j(hover)}}} + ws.axes(b)]
    + ws.basemap(bbox, b, rivers=false, admin=1),
  layout: ws.grid(bbox, rows={rows}, cols={cols}, panel=210, empty={j(empty)})
    + ws.classBar(ws.palettes.{palette}, {j(units)}, y=0.05, len=0.85)
    + {{
      title: {{text: {j(title)}, subtitle: {{text: {j(subtitle)}}}}},
      // The blend spans every row on the right, with room for its latitude labels.
      ['xaxis' + ax(b)]+: {{domain: [{small_cols} / {cols} + gap, 1 - gap / 2], showticklabels: true}},
      ['yaxis' + ax(b)]+: {{domain: [gap / 2, 1 - gap / 2], showticklabels: true}},
      annotations+: [ws.panelTitle(b, '<b>Blend</b>') + {{font: {{size: 16}}}}],
    }}
    + {{['xaxis' + ax(c)]+: {{showticklabels: false}} for c in cells}}
    + {{['yaxis' + ax(c)]+: {{showticklabels: false}} for c in cells}},
}}
"""


def _figure(bl, n, title, subtitle, palette, units):
    from weather_skills_plotting.bind import resolve
    from weather_skills_plotting.export import validate
    from weather_skills_plotting.lint import check_bindings
    from weather_skills_plotting.render import SpecArg, evaluate, house_template, input_metadata

    figure = evaluate(SpecArg(_spec(n, title, subtitle, palette, units)), {"BL": input_metadata(bl)}, {})
    figure.setdefault("layout", {}).setdefault("template", house_template())
    check_bindings(figure)
    resolved, _ = resolve(figure, {"BL": bl, "const": {}})
    return validate(resolved)


@weather_skill(name="blend-interface", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=_Inputs, action="append", required=True)
@weather_skill.argument("--label", action="append")
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--lead", type=int, default=1)
@weather_skill.argument("--weights")
@weather_skill.argument("--reference-grid")
@weather_skill.argument("--palette", default="ppt_week")
@weather_skill.argument("--title")
def blend_interface(input, label, variable, lead, weights, reference_grid, palette, title, output, **kwargs):
    """Choose blend weights for several forecasts on an interactive HTML page with coupled sliders."""
    import plotly.io as pio
    import xarray as xr

    if Path(output).suffix.lower() not in (".html", ".htm"):
        raise UsageError(f"--output {output}: the page is written to .html")
    inputs = [str(i) for i in input]
    with tempfile.TemporaryDirectory(prefix="blend-interface-") as work:
        zarr = Path(work) / "members.zarr"
        _run_blend(inputs, label, variable, lead, weights, reference_grid, zarr)
        ds = xr.open_zarr(zarr).load()

    variable = next(v for v in ds.data_vars if not v.endswith("_members"))
    members = ds[f"{variable}_members"].squeeze(drop=True)
    names = [str(m) for m in members["model"].values]
    units = str(ds[variable].attrs.get("units", ""))
    bl = xr.Dataset({f"m{k}": members.isel(model=k, drop=True) for k in range(len(names))})
    bl["blend"] = ds[variable].squeeze(drop=True)

    left_out = json.loads(ds.attrs.get("blend_left_out", "[]"))
    init = str(ds.attrs.get("weather_skills_forecast_init", ""))[:10]
    valid = next((str(ds[d].values[0])[:10] for d in ds.dims if ds[d].dtype.kind == "M"), "")
    period = ds[variable].attrs.get("aggregation_period", "")
    title = title or f"Forecast blend, lead {lead}" + (f" ({period} totals)" if period else "")
    subtitle = " · ".join(s for s in (
        f"init {init}" if init else "",
        f"valid from {valid}" if valid else "",
        f"{len(names)} forecasts on a {grid_spacing(bl['latitude'].values):g}° grid",
        f"no lead {lead}: {', '.join(left_out)}" if left_out else "",
    ) if s)

    fig = _figure(bl, len(names), title, subtitle, palette, units)
    heatmaps = [k for k, t in enumerate(fig.data) if t.type == "heatmap"]
    setup = {
        "labels": names,
        "small": heatmaps[:-1],
        "blend": heatmaps[-1],
        "axes": ["" if c == 0 else str(c + 1) for c in _layout(len(names))[3]],
        "weights": json.loads(ds.attrs["blend_weights"]),
        "record": {"skill": "blend", "inputs": inputs, **({"labels": label} if label else {}),
                   "variable": variable, "lead": lead, "init": init, "valid": valid},
    }
    page = pio.to_html(
        fig,
        include_plotlyjs=True,
        full_html=True,
        div_id="blendfig",
        post_script=_SCRIPT.replace("__SETUP__", json.dumps(setup)),
        config={"scrollZoom": True, "showSendToCloud": False, "displaylogo": False},
    )
    page = page.replace("<head>", "<head>\n<title>Forecast Blend</title>\n" + _CSS, 1)
    page = page.replace("<body>", "<body>\n" + _TOOLBAR, 1).replace("</body>", _FOOTER + "\n</body>", 1)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


_CSS = """<style>
body { margin: 0; background: #fff; color: #1b1f24; font: 13px/1.35 system-ui, -apple-system, "Segoe UI", sans-serif; }
button { font: inherit; padding: 5px 10px; border-radius: 6px; border: 1px solid #c8ced6; background: #fff; cursor: pointer; }
button:hover { border-color: #2563eb; }
button.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
.muted { color: #5b6573; font-variant-numeric: tabular-nums; }
.steps { margin: 10px 16px 0; padding: 8px 12px; max-width: 900px; background: #eef4ff; border: 1px solid #c9dafc; border-radius: 6px; }
.bar { display: flex; gap: 10px; align-items: center; padding: 10px 16px 0; flex-wrap: wrap; }
.wslider { position: absolute; display: flex; flex-direction: column; gap: 1px; z-index: 5; }
.wslider .lab { display: flex; justify-content: space-between; gap: 6px; font-weight: 600; white-space: nowrap; }
.wslider .lab span { overflow: hidden; text-overflow: ellipsis; }
.wslider output { color: #2563eb; font-variant-numeric: tabular-nums; }
.wslider input { width: 100%; margin: 0; accent-color: #2563eb; }
pre { margin: 6px 0 0; padding: 8px 10px; background: #f4f6f8; border: 1px solid #dde2e8; border-radius: 6px;
  font: 12px/1.4 ui-monospace, Menlo, monospace; white-space: pre-wrap; word-break: break-all; user-select: all; }
.footer { padding: 0 16px 20px; max-width: 1100px; }
.footer h3 { font-size: 13px; margin: 8px 0 0; }
</style>"""

_TOOLBAR = """<p class="steps"><b>To blend:</b> set the weights with the sliders, press <b>Download blend</b>,
then upload the downloaded file to your chat to generate the blended Zarr.</p>
<div class="bar"><button id="download" class="primary" type="button">Download blend</button>
<button id="equal" type="button">Reset to equal weights</button>
<span class="muted" id="sum"></span><span id="status"></span></div>
<div class="bar"><span class="muted" id="readout"></span></div>"""

_FOOTER = """<div class="footer"><h3>Weights</h3><pre id="weights"></pre></div>"""

# Runs after Plotly draws the figure; {plot_id} is the figure's div. The footer
# comes after the figure in the page, so wait for the whole document.
_SCRIPT = r"""
const start = () => {
const S = __SETUP__;
const gd = document.getElementById('{plot_id}');
const n = S.labels.length;
const $ = id => document.getElementById(id);

// Plotly stores z as packed binary ({dtype, bdata, shape}); unpack it to rows.
const TYPES = {f8: Float64Array, f4: Float32Array, i4: Int32Array, i2: Int16Array, i1: Int8Array, u1: Uint8Array};
function rows(z) {
  if (Array.isArray(z)) return z;
  const bytes = Uint8Array.from(atob(z.bdata), c => c.charCodeAt(0));
  const flat = new TYPES[z.dtype](bytes.buffer);
  const [ny, nx] = String(z.shape).split(',').map(Number);
  return Array.from({length: ny}, (_, j) => Array.from(flat.subarray(j * nx, (j + 1) * nx)));
}
const Z = S.small.map(k => rows(gd.data[k].z));

// Weights start from the address (#w=name:0.2,...), so a reload or a shared link
// keeps them, else from the weights the page was built with.
function fromHash() {
  try {
    const m = /[#&]w=([^&]*)/.exec(location.hash);
    if (!m) return null;
    const got = Object.fromEntries(decodeURIComponent(m[1]).split(',').map(p => p.split(':')));
    const v = S.labels.map(l => parseFloat(got[l]));
    const sum = v.reduce((a, b) => a + b, 0);
    return v.every(x => x >= 0) && sum > 0 ? v.map(x => x / sum) : null;
  } catch (e) { return null; }
}
let w = fromHash() || S.labels.map(l => S.weights[l]);

// Four decimals, with the rounding left over put on the largest so they add to exactly 1.
function weightMap() {
  const r = w.map(x => Math.round(x * 10000));
  r[w.indexOf(Math.max(...w))] += 10000 - r.reduce((a, b) => a + b, 0);
  return Object.fromEntries(S.labels.map((l, m) => [l, r[m] / 10000]));
}
const record = () => Object.assign({}, S.record, {weights: weightMap()});

gd.parentElement.style.position = 'relative';
const boxes = S.labels.map((lab, m) => {
  const box = document.createElement('div');
  box.className = 'wslider';
  box.innerHTML = `<div class="lab"><span title="${lab}">${lab}</span><output></output></div>
    <input type="range" min="0" max="1" step="0.01" aria-label="Weight for ${lab}">`;
  box.querySelector('input').addEventListener('input', e => setWeight(m, parseFloat(e.target.value)));
  gd.parentElement.appendChild(box);
  return box;
});

// Each slider sits in the gap above its panel.
function place() {
  const L = gd._fullLayout;
  boxes.forEach((box, m) => {
    const xa = L['xaxis' + S.axes[m]], ya = L['yaxis' + S.axes[m]];
    box.style.left = (gd.offsetLeft + xa._offset) + 'px';
    box.style.width = xa._length + 'px';
    box.style.top = (gd.offsetTop + ya._offset - 38) + 'px';
  });
}

// Moving one slider rescales the others in proportion so the weights still add to 1.
function setWeight(m, v) {
  v = Math.max(0, Math.min(1, v));
  const rest = w.reduce((a, x, k) => k === m ? a : a + x, 0);
  w = w.map((x, k) => k === m ? v : (rest > 1e-9 ? x * (1 - v) / rest : (1 - v) / (n - 1)));
  refresh();
}

// Same rule as the blend skill: blank where any forecast with weight has no value.
function blended() {
  return Z[0].map((row, j) => row.map((_, i) => {
    let s = 0;
    for (let m = 0; m < n; m++) {
      if (w[m] <= 0) continue;
      const v = Z[m][j][i];
      if (v == null || !isFinite(v)) return null;
      s += w[m] * v;
    }
    return s;
  }));
}

function refresh() {
  boxes.forEach((box, m) => {
    box.querySelector('input').value = w[m].toFixed(2);
    box.querySelector('output').textContent = w[m].toFixed(2);
    box.style.opacity = w[m] < 0.005 ? 0.55 : 1;
  });
  $('sum').textContent = 'Sum of weights: ' + w.reduce((a, b) => a + b, 0).toFixed(2);
  $('weights').textContent = JSON.stringify(weightMap());
  Plotly.restyle(gd, {z: [blended()]}, [S.blend]);
  try {
    const tag = S.labels.map((l, m) => `${l}:${w[m].toFixed(4)}`).join(',');
    history.replaceState(null, '', '#w=' + encodeURIComponent(tag));
  } catch (e) {}
}

// Download blend saves the record as a file to upload to the chat, and also posts it
// to the page that embeds this one, for hosts that listen.
$('download').addEventListener('click', () => {
  const r = record();
  const name = `blend_weights_${r.init || 'init'}_lead${r.lead}.json`;
  try {
    const url = URL.createObjectURL(new Blob([JSON.stringify(r, null, 1)], {type: 'application/json'}));
    const a = Object.assign(document.createElement('a'), {href: url, download: name});
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $('status').textContent = `Downloaded ${name}: upload it to your chat.`;
  } catch (e) {
    $('status').textContent = 'The download was blocked; paste the weights below into your chat instead.';
  }
  try { if (window.parent !== window) window.parent.postMessage({type: 'weather-skills-blend', record: r}, '*'); } catch (e) {}
});
$('equal').addEventListener('click', () => { w = new Array(n).fill(1 / n); refresh(); });

// Hovering the blend lists what each forecast has in that cell.
gd.on('plotly_hover', ev => {
  const p = ev.points[0];
  if (p.curveNumber !== S.blend) return;
  const [j, i] = p.pointIndex;
  $('readout').textContent = '· ' + S.labels.map((l, m) => {
    const v = Z[m][j][i];
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
