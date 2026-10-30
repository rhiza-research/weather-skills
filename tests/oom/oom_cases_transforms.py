"""Transform cases for the OOM suite, shared by every tier.

Each case reads one or more synthetic stores (oom_synthetic) whose size the
tier picks. Args are fixed strings valid at every size; ``{tmp}`` in an arg is
the test's tmp dir, where ``FILES`` are written first.
"""

from __future__ import annotations

import json
from pathlib import Path

from oom_harness import Input, TransformCase, zarr_nbytes
from oom_synthetic import synthetic_zarr

# A large polygon over Africa and Europe, for clip-region --geojson.
_POLYGON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"name": "afro-eurasia"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-20, -40], [55, -40], [70, 30], [40, 70], [-15, 60], [-20, -40]]],
            },
        }
    ],
}
FILES = {"polygon.geojson": json.dumps(_POLYGON)}

GRID = Input.of("-i", "grid")
GRID_B = Input.of("-i", "grid", seed=1)
T2M = Input.of("-i", "t2m_grid")
FORECAST = Input.of("-i", "ensemble_forecast")  # tp accumulated since init (mm)
FORECAST_RATE = Input.of("-i", "ensemble_forecast", accumulated=False)  # tp in mm day-1
STEPS_0_39 = tuple(a for k in range(40) for a in ("--index", str(k)))
POINTS = (
    "--point",
    "-1.29,36.82,nairobi",
    "--point",
    "51.5,-0.12,london",
    "--point",
    "-33.9,151.2,sydney",
)

CASES = [
    # --- aggregate-temporal
    TransformCase(
        "aggregate-temporal-period",
        "aggregate-temporal",
        "aggregate.py",
        inputs=(GRID,),
        args=("--period", "weekly"),
    ),
    TransformCase(
        "aggregate-temporal-window",
        "aggregate-temporal",
        "aggregate.py",
        inputs=(GRID,),
        args=("--window", "weekly", "--method", "max"),
    ),
    TransformCase(
        "aggregate-temporal-step",
        "aggregate-temporal",
        "aggregate.py",
        inputs=(FORECAST_RATE,),
        args=("--period", "weekly"),
    ),
    # --- clip-region
    TransformCase(
        "clip-region", "clip-region", "clip.py", inputs=(GRID,), args=("--bbox", "40/-20/-40/60")
    ),
    TransformCase(
        "clip-region-geojson",
        "clip-region",
        "clip.py",
        inputs=(GRID,),
        args=("--geojson", "{tmp}/polygon.geojson"),
    ),
    TransformCase(
        "clip-region-geojson-keep-outside",
        "clip-region",
        "clip.py",
        inputs=(GRID,),
        args=("--geojson", "{tmp}/polygon.geojson", "--keep-outside"),
    ),
    # --- coarsen (4 deg is coarser than both the smoke and the full grid)
    TransformCase(
        "coarsen-linear",
        "coarsen",
        "coarsen.py",
        inputs=(GRID,),
        args=("--target-resolution", "4", "--offset", "0"),
    ),
    TransformCase(
        "coarsen-conservative",
        "coarsen",
        "coarsen.py",
        inputs=(GRID,),
        args=("--target-resolution", "4", "--offset", "0", "--method", "conservative"),
    ),
    # --- concat
    TransformCase(
        "concat-time",
        "concat",
        "concat.py",
        inputs=(GRID, Input.of("-i", "grid", start="2030-01-01")),
        args=("--dim", "time"),
    ),
    TransformCase(
        "concat-new-dim",
        "concat",
        "concat.py",
        inputs=(GRID, GRID_B),
        args=("--dim", "model", "--coords", "a,b"),
    ),
    # --- convert-calendar
    TransformCase(
        "convert-calendar",
        "convert-calendar",
        "convert_calendar.py",
        inputs=(GRID,),
        args=("--calendar", "noleap"),
    ),
    # --- convert-to-totals
    TransformCase(
        "convert-to-totals",
        "convert-to-totals",
        "convert_to_totals.py",
        inputs=(Input.of("-i", "grid", period_days=1),),
    ),
    # --- csv-io export (a tidy table of a 3 GiB grid would be ~25 GB of text; pivot by time)
    TransformCase(
        "csv-io-export",
        "csv-io",
        "csv_io.py",
        inputs=(Input.of("-i", "point_obs"),),
        args=("--pivot", "time=precip", "--max-rows", "100000000000"),
        output_name="out.csv",
        streams=False,
    ),
    # --- deaccumulate
    TransformCase("deaccumulate", "deaccumulate", "deaccumulate.py", inputs=(FORECAST,)),
    # --- difference
    TransformCase("difference", "difference", "difference.py", inputs=(GRID, GRID_B)),
    # --- downscale
    TransformCase(
        "downscale-linear",
        "downscale",
        "downscale.py",
        inputs=(GRID,),
        args=("--algorithm", "linear-interpolation", "--factor", "2"),
    ),
    TransformCase(
        "downscale-qq",
        "downscale",
        "downscale.py",
        inputs=(GRID, Input.of("--qq-reference", "grid", seed=1)),
        args=("--algorithm", "q-q", "--factor", "1"),
    ),
    # --- drop
    TransformCase(
        "drop",
        "drop",
        "drop.py",
        inputs=(Input.of("-i", "climatology"),),
        args=("--name", "precip_std"),
    ),
    # --- edit-coords
    TransformCase(
        "edit-coords",
        "edit-coords",
        "edit_coords.py",
        inputs=(GRID,),
        args=("--rename", "precip=rain"),
    ),
    # --- indicator
    TransformCase(
        "indicator-onset",
        "indicator",
        "indicator.py",
        inputs=(GRID,),
        args=("--rule", "icpac-onset", "--detect", "first"),
    ),
    TransformCase(
        "indicator-probability",
        "indicator",
        "indicator.py",
        inputs=(FORECAST_RATE,),
        args=("--rule", "tp sum 3d >= 20", "--probability"),
    ),
    # --- inspect-zarr (prints a summary, writes nothing)
    TransformCase("inspect-zarr", "inspect-zarr", "inspect_zarr.py", inputs=(GRID,), output=False),
    # --- label-group
    TransformCase(
        "label-group-time",
        "label-group",
        "label_group.py",
        inputs=(GRID,),
        args=("--time", "month", "--time", "year"),
    ),
    TransformCase(
        "label-group-country",
        "label-group",
        "label_group.py",
        inputs=(GRID,),
        args=("--level", "country"),
    ),
    # --- local-extrema
    TransformCase(
        "local-extrema",
        "local-extrema",
        "local_extrema.py",
        # Persistent blobs: on noise the extrema table grows with time squared.
        inputs=(Input.of("-i", "grid", smooth=True),),
        args=("--kind", "high"),
        lazy_output=False,  # reads one 2-D map at a time; the extrema table is built in memory
    ),
    # --- merge
    TransformCase("merge", "merge", "merge.py", inputs=(GRID, T2M)),
    # --- netcdf-io export
    TransformCase(
        "netcdf-io-export", "netcdf-io", "netcdf_io.py", inputs=(GRID,), output_name="out.nc"
    ),
    # --- point-value
    TransformCase("point-value", "point-value", "point_value.py", inputs=(GRID,), args=POINTS),
    TransformCase(
        "point-value-bilinear",
        "point-value",
        "point_value.py",
        inputs=(GRID,),
        args=(*POINTS, "--method", "bilinear"),
    ),
    TransformCase(
        "point-value-max-distance",
        "point-value",
        "point_value.py",
        inputs=(GRID,),
        args=(*POINTS, "--max-distance", "5"),
    ),
    TransformCase(
        "point-value-points",
        "point-value",
        "point_value.py",
        inputs=(GRID, Input.of("--points", "point_obs")),
    ),
    # --- select (step has 46 entries at every size; keep 40 of them)
    TransformCase(
        "select-index",
        "select",
        "select_dim.py",
        inputs=(FORECAST,),
        args=("--dim", "step", *STEPS_0_39),
    ),
    TransformCase(
        "select-value",
        "select",
        "select_dim.py",
        inputs=(FORECAST,),
        args=("--dim", "step", *(a for k in range(40) for a in ("--value", f"{k}D"))),
    ),
    # --- standardize-anomaly
    TransformCase(
        "standardize-anomaly",
        "standardize-anomaly",
        "standardize_anomaly.py",
        inputs=(GRID, Input.of("--climatology", "climatology")),
        args=("-v", "precip"),
    ),
    # --- step-to-time
    TransformCase("step-to-time", "step-to-time", "step_to_time.py", inputs=(FORECAST,)),
    # --- summarize-dim
    TransformCase(
        "summarize-dim-mean",
        "summarize-dim",
        "summarize_dim.py",
        inputs=(GRID,),
        args=("--dim", "time", "--method", "mean"),
    ),
    TransformCase(
        "summarize-dim-quantile",
        "summarize-dim",
        "summarize_dim.py",
        inputs=(FORECAST_RATE,),
        args=("--dim", "number", "--method", "quantile", "-q", "0.1", "-q", "0.5", "-q", "0.9"),
    ),
    TransformCase(
        "summarize-dim-by-region",
        "summarize-dim",
        "summarize_dim.py",
        inputs=(Input.of("-i", "grid", labels=True),),
        args=("--by", "region", "--method", "mean"),
    ),
    TransformCase(
        "summarize-dim-by-month",
        "summarize-dim",
        "summarize_dim.py",
        inputs=(Input.of("-i", "grid", labels=True),),
        args=("--by", "month", "--method", "max", "--min-coverage", "0"),
    ),
    TransformCase(
        "summarize-dim-by-month-coverage",
        "summarize-dim",
        "summarize_dim.py",
        inputs=(Input.of("-i", "grid", labels=True),),
        args=("--by", "month", "--method", "mean"),
    ),
    # --- unit-convert
    TransformCase(
        "unit-convert-water-density",
        "unit-convert",
        "unit-convert.py",
        inputs=(GRID,),
        args=("--to-units", "kg m-2 s-1"),
    ),
    TransformCase(
        "unit-convert-temperature",
        "unit-convert",
        "unit-convert.py",
        inputs=(T2M,),
        args=("--to-units", "degC"),
    ),
    TransformCase(
        "unit-convert-to-standard",
        "unit-convert",
        "unit-convert.py",
        inputs=(T2M,),
        args=("--to-standard",),
    ),
    # --- verify
    TransformCase(
        "verify-hits",
        "verify",
        "verify.py",
        inputs=(Input.of("--forecast", "grid"), Input.of("--obs", "grid", seed=1)),
    ),
    TransformCase(
        "verify-mae",
        "verify",
        "verify.py",
        inputs=(Input.of("--forecast", "grid"), Input.of("--obs", "grid", seed=1)),
        args=("--metric", "mae"),
    ),
    # --- zonal-moisture-transport
    TransformCase(
        "zonal-moisture-transport",
        "zonal-moisture-transport",
        "zonal_moisture_transport.py",
        inputs=(Input.of("-i", "pressure_levels"),),
    ),
    TransformCase(
        "zonal-moisture-transport-per-level",
        "zonal-moisture-transport",
        "zonal_moisture_transport.py",
        inputs=(Input.of("-i", "pressure_levels"),),
        args=("--no-integrate",),
    ),
]


def transform_argv(case: TransformCase, size: int, cache: Path, tmp: Path):
    """``(args, input_bytes, output)`` for ``case`` with inputs of ``size`` bytes each."""
    for name, text in FILES.items():
        (tmp / name).write_text(text)
    args, input_bytes = [], 0
    for item in case.inputs:
        path = synthetic_zarr(item.kind, size, cache, **dict(item.variant))
        input_bytes += zarr_nbytes(path)
        args += [item.flag, str(path)]
    args += [a.format(tmp=tmp) for a in case.args]
    output = tmp / case.output_name if case.output else None
    if output is not None:
        args += ["-o", str(output)]
    return args, input_bytes, output
