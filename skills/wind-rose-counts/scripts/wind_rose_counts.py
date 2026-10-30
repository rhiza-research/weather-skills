# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.2",
# ]
# ///
"""Count wind samples by direction sector and speed class: the table behind a wind rose."""

import sys

import numpy as np
import xarray as xr
from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import dequantify_dataset, units_equal

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

# Default speed classes in m s-1; the last class is open-ended.
_DEFAULT_EDGES_MS = (0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0)


@weather_skill(name="wind-rose-counts", version=_SKILL_VERSION)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--speed", default="wind_speed", help="Wind speed variable.")
@weather_skill.argument(
    "--direction",
    default="wind_from_direction",
    help="Variable holding the direction the wind blows from, degrees clockwise from north.",
)
@weather_skill.argument(
    "--sectors", type=int, default=16, help="Number of direction sectors (default 16)."
)
@weather_skill.argument(
    "--speed-edges",
    help="Comma-separated lower edges of the speed classes, from 0 (default 0,2,4,...,12 m s-1).",
)
@weather_skill.argument(
    "--calm-speed",
    type=float,
    default=1e-6,
    help="Speeds at or below this count as calm, direction or not (default 1e-6).",
)
@weather_skill.argument(
    "--keep",
    action="append",
    default=[],
    help="Dimension to keep instead of pooling into the counts (repeatable, e.g. station_id).",
)
def wind_rose_counts(ds, speed, direction, sectors, speed_edges, calm_speed, keep, **kwargs):
    """Count wind samples by direction sector and speed class: the table behind a wind rose."""
    ds = dequantify_dataset(ds)
    missing = [name for name in (speed, direction) if name not in ds.data_vars]
    if missing:
        raise UsageError(
            f"{missing} not in dataset (have {list(ds.data_vars)}); "
            "wind-speed-direction makes wind_speed and wind_from_direction from u and v"
        )
    spd, dirn = ds[speed], ds[direction]
    if spd.dims != dirn.dims:
        raise UsageError(f"{speed!r} has dims {spd.dims} but {direction!r} has dims {dirn.dims}")
    if not units_equal(dirn.attrs.get("units"), "degree"):
        raise UsageError(
            f"{direction!r} is in {dirn.attrs.get('units')!r}; directions must be in degree"
        )
    units = spd.attrs.get("units")
    if not (isinstance(units, str) and units.strip()):
        raise UsageError(f"variable {speed!r} has no units attr")
    unknown = [d for d in keep if d not in spd.dims]
    if unknown:
        raise UsageError(f"--keep {unknown} not among the dims of {speed!r}: {spd.dims}")
    if sectors < 4:
        raise UsageError(f"--sectors must be at least 4, got {sectors}")
    if not calm_speed >= 0:
        raise UsageError(f"--calm-speed must be zero or more, got {calm_speed}")

    if speed_edges is None:
        if not units_equal(units, "m s-1"):
            raise UsageError(
                f"{speed!r} is in {units!r}; pass --speed-edges in those units, "
                "or convert to m s-1 with unit-convert for the default classes"
            )
        edges = np.asarray(_DEFAULT_EDGES_MS)
    else:
        try:
            edges = np.asarray([float(x) for x in speed_edges.split(",")])
        except ValueError:
            raise UsageError(f"--speed-edges {speed_edges!r} is not a list of numbers") from None
        if edges[0] != 0 or np.any(np.diff(edges) <= 0):
            raise UsageError(f"--speed-edges must start at 0 and increase, got {speed_edges!r}")

    # Kept dims first, pooled dims flattened into one sample axis.
    pooled = [d for d in spd.dims if d not in keep]
    spd = spd.transpose(*keep, *pooled)
    dirn = dirn.transpose(*keep, *pooled)
    shape = tuple(spd.sizes[d] for d in keep)
    s = spd.values.reshape(int(np.prod(shape)), -1)
    d = dirn.values.reshape(int(np.prod(shape)), -1)

    width = 360.0 / sectors
    upper = np.append(edges[1:], np.inf)
    counts = np.zeros((s.shape[0], sectors, edges.size))
    calms = np.zeros(s.shape[0])
    no_direction = 0
    for k in range(s.shape[0]):
        calm = s[k] <= calm_speed
        windy = (s[k] > calm_speed) & np.isfinite(d[k])
        no_direction += int(((s[k] > calm_speed) & ~np.isfinite(d[k])).sum())
        calms[k] = calm.sum()
        # Sector 0 is centred on north: it runs from -width/2 to +width/2.
        sector = np.floor(((d[k][windy] + width / 2) % 360.0) / width).astype(int) % sectors
        speed_class = np.searchsorted(edges, s[k][windy], side="right") - 1
        np.add.at(counts[k], (sector, speed_class), 1)
    if no_direction:
        print(
            f"Warning: left out {no_direction} sample(s) with {speed} above --calm-speed "
            f"but no {direction}",
            file=sys.stderr,
        )
    total = counts.sum(axis=(1, 2)) + calms
    with np.errstate(invalid="ignore", divide="ignore"):
        frequency = 100.0 * counts / total[:, None, None]
        calm_frequency = 100.0 * calms / total

    keep_coords = {
        name: coord
        for name, coord in ds[speed].coords.items()
        if set(coord.dims) <= set(keep) and coord.dims
    }
    rose_dims = (*keep, "direction", "speed_bin")
    out = xr.Dataset(
        {
            "count": (rose_dims, counts.reshape(*shape, sectors, edges.size)),
            "frequency": (rose_dims, frequency.reshape(*shape, sectors, edges.size)),
            "calm_count": (keep, calms.reshape(shape)),
            "calm_frequency": (keep, calm_frequency.reshape(shape)),
            "sample_count": (keep, total.reshape(shape)),
        },
        coords={
            **keep_coords,
            "direction": ("direction", np.arange(sectors) * width),
            "speed_bin": ("speed_bin", edges),
            "speed_upper": ("speed_bin", upper),
        },
        attrs=ds.attrs,
    )
    out["direction"].attrs = {
        "units": "degree",
        "long_name": f"centre of the {width:g} degree sector the wind blows from",
    }
    out["speed_bin"].attrs = {"units": units, "long_name": f"lower edge of the {speed} class"}
    out["speed_upper"].attrs = {
        "units": units,
        "long_name": f"upper edge of the {speed} class (exclusive; inf for the last)",
    }
    pooled_text = ", ".join(pooled) if pooled else "nothing"
    out["count"].attrs = {
        "units": "1",
        "long_name": f"samples in each direction sector and speed class, pooled over {pooled_text}",
    }
    out["frequency"].attrs = {
        "units": "percent",
        "long_name": "count as a percent of all samples, calms included",
    }
    out["calm_count"].attrs = {
        "units": "1",
        "long_name": f"calm samples ({speed} <= {calm_speed:g} {units})",
    }
    out["calm_frequency"].attrs = {
        "units": "percent",
        "long_name": "calm samples as a percent of all samples",
    }
    out["sample_count"].attrs = {
        "units": "1",
        "long_name": "calm samples plus samples in the sectors",
    }
    return out


if __name__ == "__main__":
    wind_rose_counts()
