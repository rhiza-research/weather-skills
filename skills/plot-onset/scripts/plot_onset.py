# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cartopy",
#   "cf-xarray",
#   "cftime>=1.6",
#   # matplotlib<3.10: cartopy gridliner crash
#   "matplotlib>=3.8,<3.10",
#   "numpy",
#   "xarray",
#   "zarr",
#   "pint-xarray>=0.6",
# ]
# ///
"""Onset map: ensemble-mean onset date plus per-cell member agreement.

Renders one PNG carrying both halves of an ensemble onset forecast: the
mean onset date as a discrete date colormap, and how many members actually
found an onset at all. Cells where few members triggered are faded and the
member percentage is drawn over the map (per-cell text on a coarse grid,
contour lines on a fine one), so a mean resting on a handful of members
can't be read as confident.
"""

import datetime
import sys
from pathlib import Path

from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import ALIASES, MEMBER, detect_spatial_dims
from weather_skills_core.standard_utils import parse_date

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.0"

# Five main color bands, each split into light->dark shades by _discrete_cmap.
_SEGMENTS = [
    ("#EFDFC0", "#8B5A2B"),  # sand
    ("#B9E3A8", "#1B5E20"),  # green
    ("#A9F0EC", "#00838F"),  # cyan
    ("#F3BEDE", "#7B2D8E"),  # pink-purple
    ("#E3E3E3", "#4D4D4D"),  # gray
]


def _discrete_cmap(vmin, vmax, n_shades=4):
    """Discrete date colormap: five color bands, each split into n_shades
    light->dark steps. Returns (cmap, norm, boundaries, segment_edges)."""
    import numpy as np
    from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, ListedColormap

    segment_edges = np.linspace(vmin, vmax, len(_SEGMENTS) + 1)

    colors = []
    boundaries = [segment_edges[0]]
    for i, (c_light, c_dark) in enumerate(_SEGMENTS):
        seg_cmap = LinearSegmentedColormap.from_list("", [c_light, c_dark])
        # sampled at bin centers so the shades within a band are evenly spaced
        colors.extend(seg_cmap((np.arange(n_shades) + 0.5) / n_shades))
        sub_edges = np.linspace(segment_edges[i], segment_edges[i + 1], n_shades + 1)[1:]
        boundaries.extend(sub_edges)

    cmap = ListedColormap(colors, name="onset_bands_discrete")
    return cmap, BoundaryNorm(boundaries, ncolors=cmap.N), np.array(boundaries), segment_edges


def _resolve_member_dim(ds):
    """The ensemble dim (``number``/``member``/``realization``), or None."""
    return next((d for d in ds.dims if ALIASES.get(d) == MEMBER), None)


def _resolve_onset_variable(ds, variable):
    """The datetime64 onset variable to map.

    Onset results carry absolute dates only after ``step-to-time`` has run
    upstream of ``onset-date``; a timedelta64 (elapsed lead time) result has
    no calendar date to place on a date scale, so it is rejected with that
    pointer rather than silently plotted against an arbitrary origin.
    """
    import numpy as np

    def is_date(name):
        return np.issubdtype(ds[name].dtype, np.datetime64)

    def is_duration(name):
        return np.issubdtype(ds[name].dtype, np.timedelta64)

    if variable is not None:
        if variable not in ds.data_vars:
            raise UsageError(
                f"--variable '{variable}' not a data variable of the input. "
                f"Valid data variables: {list(ds.data_vars)}"
            )
        if not is_date(variable):
            hint = (
                " It is a lead-time duration; run step-to-time on the forecast before "
                "onset-date so the onset comes out as an absolute date."
                if is_duration(variable)
                else ""
            )
            raise UsageError(
                f"--variable '{variable}' is {ds[variable].dtype}, not a datetime64 "
                f"onset date.{hint}"
            )
        return variable

    dates = [v for v in ds.data_vars if is_date(v)]
    if len(dates) == 1:
        return dates[0]
    if not dates:
        durations = [v for v in ds.data_vars if is_duration(v)]
        if durations:
            raise UsageError(
                f"no datetime64 onset variable; {durations} are lead-time durations. "
                "Run step-to-time on the forecast before onset-date so the onset comes "
                "out as an absolute date."
            )
        raise UsageError(
            f"no datetime64 onset variable in {list(ds.data_vars)}. Run onset-date first."
        )
    raise UsageError(f"several datetime64 variables {dates}; pick one with --variable.")


def _draw_pct_contours(ax, crs, lon2d, lat2d, pct_vals):
    """Contour the member-agreement field on a grid too fine for per-cell text."""
    import numpy as np

    finite = pct_vals[np.isfinite(pct_vals)]
    if finite.size < 4:
        return
    # levels fit to this source's own range rather than a fixed 25/50/75 — a
    # source whose members rarely agree would otherwise show no lines at all
    levels = np.linspace(finite.min(), finite.max(), 5)[1:-1]
    if len(levels) < 2 or levels[-1] <= levels[0]:
        return

    cs = ax.contour(
        lon2d, lat2d, pct_vals, levels=levels, colors="red", linewidths=1.5, transform=crs
    )

    # One label per level, on its longest segment. Edge levels are pinned to
    # the top of their band and inner levels to the bottom, and the first
    # level is pushed right while the rest go left, so a typical 3-level set
    # spreads across the corners instead of clustering on one side.
    n_levels = len(cs.allsegs)
    label_pos = []
    for i, segs in enumerate(cs.allsegs):
        longest = max((s for s in segs if len(s) >= 2), key=len, default=None)
        if longest is None:
            continue
        lat = longest[:, 1]
        target_lat = lat.max() if i in (0, n_levels - 1) else lat.min()
        span = (lat.max() - lat.min()) * 0.1 or 1e-6
        candidates = longest[np.isclose(lat, target_lat, atol=span)]
        lon_idx = np.argmax(candidates[:, 0]) if i == 0 else np.argmin(candidates[:, 0])
        label_pos.append(tuple(candidates[lon_idx]))

    if not label_pos:
        return
    clabels = ax.clabel(cs, inline=True, fontsize=12, fmt="%d%%", colors="white", manual=label_pos)

    # Nudge each label off its line, alternating side, and clamp to the axes'
    # rendered extent so a label can never land outside the visible plot.
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    shift = 0.02 * (xlim[1] - xlim[0])
    margin_x = 0.06 * (xlim[1] - xlim[0])
    margin_y = 0.06 * (ylim[1] - ylim[0])
    for i, lbl in enumerate(clabels):
        lbl.set_bbox({"facecolor": "black", "edgecolor": "none", "pad": 1})
        lbl.set_rotation(0)
        x, y = lbl.get_position()
        x += shift if i % 2 == 0 else -shift
        lbl.set_position(
            (
                min(max(x, xlim[0] + margin_x), xlim[1] - margin_x),
                min(max(y, ylim[0] + margin_y), ylim[1] - margin_y),
            )
        )


@weather_skill(
    name="plot-onset",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), required=True)
@weather_skill.argument(
    "--variable",
    "-v",
    default=None,
    help="Onset variable to map (must be datetime64). Default: the input's only "
    "datetime64 data variable.",
)
@weather_skill.argument(
    "--start-date",
    default=None,
    help="Date the color scale starts at, YYYY-MM-DD — normally the forecast's first "
    "day. Sets the scale, it does NOT subset the data; onsets before it are drawn in "
    "the first color. Default: the earliest onset in the data (a data-derived scale is "
    "not comparable across forecasts).",
)
@weather_skill.argument(
    "--end-date",
    default=None,
    help="Date the color scale ends at, YYYY-MM-DD — normally the last day that still "
    "left a full onset search window. Sets the scale, it does NOT subset the data. "
    "Default: the latest mean onset in the data.",
)
@weather_skill.argument(
    "--low-confidence-pct",
    type=float,
    default=10.0,
    help="Fade cells where fewer than this percentage of members found an onset. Default: 10.",
)
@weather_skill.argument("--title", default=None, help="Optional plot title.")
def plot_onset(ds, variable, start_date, end_date, low_confidence_pct, title, output, **kwargs):
    """Onset map: ensemble-mean onset date plus per-cell member agreement."""
    import matplotlib

    matplotlib.use("Agg")
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import matplotlib.patheffects as pe
    import matplotlib.pyplot as plt
    import numpy as np

    variable = _resolve_onset_variable(ds, variable)
    lat_dim, lon_dim = detect_spatial_dims(ds)
    member_dim = _resolve_member_dim(ds)

    da = ds[variable]
    extra = [d for d in da.dims if d not in (lat_dim, lon_dim, member_dim)]
    if extra:
        raise UsageError(
            f"'{variable}' carries extra dim(s) {extra} beyond lat/lon"
            f"{f'/{member_dim}' if member_dim else ''}; reduce them with select first."
        )

    start = parse_date(start_date) if start_date else None
    end = parse_date(end_date) if end_date else None
    if start and end and end <= start:
        raise UsageError(f"--end-date {end} must be after --start-date {start}.")

    valid = da.values[~np.isnat(da.values)] if da.size else np.array([], dtype="datetime64[ns]")
    if valid.size == 0:
        raise DataError(
            f"'{variable}' is NaT everywhere — no member found an onset anywhere on the "
            "grid, so there is nothing to map."
        )

    # Everything downstream works in days since a reference date rather than
    # day-of-year: a forecast window that crosses New Year (a Nov/Dec init over
    # southern Africa, say) wraps day-of-year back to 1 and would otherwise
    # average to nonsense and invert the color scale.
    ref_date = start or valid.min().astype("datetime64[D]").astype(datetime.date)
    ref = np.datetime64(ref_date, "ns")
    offset = (da - ref) / np.timedelta64(1, "D")

    if member_dim:
        mean_offset = offset.mean(dim=member_dim, skipna=True)
        pct_valid = offset.notnull().mean(dim=member_dim) * 100
    else:
        mean_offset = offset
        pct_valid = None

    mean_vals = mean_offset.values
    vmin = 0.0 if start else float(np.nanmin(mean_vals))
    vmax = float((end - ref_date).days) if end else float(np.nanmax(mean_vals))
    if vmax <= vmin:
        vmax = vmin + 1  # BoundaryNorm needs a non-degenerate range
    if not start or not end:
        print(
            "Note: color scale derived from the data "
            f"({'--start-date' if not start else '--end-date'} not given), so it is not "
            "comparable across forecasts. Pass both to fix it to the forecast window.",
            file=sys.stderr,
        )

    cmap, norm, boundaries, segment_edges = _discrete_cmap(vmin, vmax)

    fig, ax = plt.subplots(figsize=(9, 7), subplot_kw={"projection": ccrs.PlateCarree()})
    mesh = mean_offset.plot.pcolormesh(
        x=lon_dim,
        y=lat_dim,
        ax=ax,
        cmap=cmap,
        norm=norm,
        transform=ccrs.PlateCarree(),
        add_colorbar=False,
    )

    if pct_valid is not None:
        # Cells backed by only a small minority of members are unreliable —
        # fade them rather than drawing them as an equally-confident color.
        # Cells that are already NaN (no onset at all) render transparent.
        mean_ll = mean_offset.transpose(lat_dim, lon_dim)
        pct_ll = pct_valid.transpose(lat_dim, lon_dim)
        low_confidence = ((pct_ll < low_confidence_pct) & mean_ll.notnull()).values.ravel()

        mesh.update_scalarmappable()
        facecolors = mesh.get_facecolor()
        facecolors[low_confidence, -1] = 0.5
        mesh.set_facecolor(facecolors)
        # Collection.draw() re-runs update_scalarmappable() on every draw, which
        # would recompute facecolors from cmap(norm(array)) and wipe the alpha
        # edit. set_array(None) is the usual escape hatch, but cartopy's
        # GeoQuadMesh assumes a real array and breaks on None — so neutralize
        # this instance's update_scalarmappable instead.
        mesh.update_scalarmappable = lambda: None

    ax.coastlines(resolution="10m", linewidth=0.8)
    ax.add_feature(cfeature.BORDERS, linewidth=0.6)
    gl = ax.gridlines(draw_labels=True, linewidth=0.3, color="gray", alpha=0.5, linestyle="--")
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {"size": 11}
    gl.ylabel_style = {"size": 11}
    ax.set_title(title or f"Onset date — {variable}", fontsize=15, fontweight="bold", pad=10)

    if pct_valid is not None:
        lon2d, lat2d = np.meshgrid(pct_valid[lon_dim].values, pct_valid[lat_dim].values)
        pct_vals = pct_valid.transpose(lat_dim, lon_dim).values
        # per-cell text only stays legible on a coarse grid (e.g. S2S); a fine
        # grid (e.g. GEFS's ~0.25deg) gets contours of the same field instead
        if pct_valid.sizes[lat_dim] * pct_valid.sizes[lon_dim] <= 200:
            for i in range(lat2d.shape[0]):
                for j in range(lat2d.shape[1]):
                    if np.isfinite(pct_vals[i, j]):
                        ax.text(
                            lon2d[i, j],
                            lat2d[i, j],
                            f"{pct_vals[i, j]:.0f}%",
                            transform=ccrs.PlateCarree(),
                            ha="center",
                            va="center",
                            fontsize=18,
                            color="black",
                            path_effects=[pe.withStroke(linewidth=2, foreground="white")],
                        )
        else:
            _draw_pct_contours(ax, ccrs.PlateCarree(), lon2d, lat2d, pct_vals)

    cbar = fig.colorbar(
        mesh,
        ax=ax,
        orientation="vertical",
        pad=0.03,
        shrink=0.85,
        aspect=25,
        boundaries=boundaries,
        ticks=segment_edges,
    )
    ref_dt = datetime.datetime.combine(ref_date, datetime.time())
    cbar.set_ticklabels(
        [(ref_dt + datetime.timedelta(days=float(t))).strftime("%b %d") for t in segment_edges]
    )
    cbar.set_label("Onset date", fontsize=13)
    cbar.ax.tick_params(labelsize=11)

    fig.tight_layout()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


if __name__ == "__main__":
    plot_onset()
