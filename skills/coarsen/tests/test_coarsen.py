"""Correctness tests for coarsen."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.standard_utils import normalize_latlon_coords
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def coarsen():
    return load_skill("coarsen", "coarsen").coarsen


def test_coarsen_reduces_spatial_dims(tmp_path, coarsen):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        coarsen,
        "-i",
        str(src),
        "-o",
        str(out),
        "--target-resolution",
        "2.0",
        "--offset",
        "0.0",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["latitude"] < 3
    assert ds.sizes["longitude"] < 4
    assert units_equal(ds["precip"].attrs.get("units"), "mm day-1")
    assert load_history(out)[-1]["skill"] == "coarsen"


def test_coarsen_rejects_finer_target(tmp_path, coarsen):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            coarsen,
            "-i",
            str(src),
            "-o",
            str(out),
            "--target-resolution",
            "0.5",
            "--offset",
            "0.0",
        )
    assert exc.value.code == 2


def test_coarsen_reference_grid_copies_exact_coords(tmp_path, coarsen):
    """Matching another Zarr must reuse its lat/lon values, not rebuild floats."""
    import numpy as np

    fine = make_gridded(
        lats=(0.0, 0.5, 1.0, 1.5, 2.0),
        lons=(10.0, 10.5, 11.0, 11.5, 12.0),
        fill=1.0,
    )
    # Values that survive the 5-decimal snap but are not k*0.5 reconstructions.
    ref_lats = np.array([0.05123, 1.05234, 2.05345], dtype=np.float64)
    ref_lons = np.array([10.05123, 11.05234, 12.05345], dtype=np.float64)
    ref = make_gridded(lats=tuple(ref_lats), lons=tuple(ref_lons), fill=2.0)
    src = write_zarr(fine, tmp_path / "fine.zarr")
    ref_path = write_zarr(ref, tmp_path / "ref.zarr")
    out = tmp_path / "out.zarr"

    run_skill(coarsen, "-i", str(src), "-o", str(out), "--reference-grid", str(ref_path))

    result = xr.open_zarr(out, consolidated=True)
    expected = normalize_latlon_coords(xr.open_zarr(ref_path, consolidated=True))
    assert np.array_equal(result["latitude"].values, expected["latitude"].values)
    assert np.array_equal(result["longitude"].values, expected["longitude"].values)


def test_coarsen_reference_grid_rejects_finer(tmp_path, coarsen):
    coarse = write_zarr(
        make_gridded(lats=(0.0, 2.0), lons=(10.0, 12.0), fill=1.0),
        tmp_path / "coarse.zarr",
    )
    fine = write_zarr(
        make_gridded(lats=(0.0, 1.0, 2.0), lons=(10.0, 11.0, 12.0), fill=1.0),
        tmp_path / "fine.zarr",
    )
    with pytest.raises(SystemExit) as exc:
        run_skill(
            coarsen,
            "-i",
            str(coarse),
            "-o",
            str(tmp_path / "out.zarr"),
            "--reference-grid",
            str(fine),
        )
    assert exc.value.code == 2


def test_coarsen_reference_grid_allows_lateral_shift(tmp_path, coarsen):
    """Same 0.05° spacing, half-cell lon offset (Kenya downscaled vs CHIRPS)."""
    import numpy as np

    src = write_zarr(
        make_gridded(
            lats=(0.00, 0.05, 0.10, 0.15),
            lons=(10.00, 10.05, 10.10, 10.15),
            fill=1.0,
        ),
        tmp_path / "src.zarr",
    )
    ref = write_zarr(
        make_gridded(
            lats=(0.00, 0.05, 0.10, 0.15),
            lons=(10.025, 10.075, 10.125, 10.175),
            fill=2.0,
        ),
        tmp_path / "ref.zarr",
    )
    out = tmp_path / "out.zarr"
    run_skill(coarsen, "-i", str(src), "-o", str(out), "--reference-grid", str(ref))
    result = xr.open_zarr(out, consolidated=True)
    expected = normalize_latlon_coords(xr.open_zarr(ref, consolidated=True))
    assert np.array_equal(result["latitude"].values, expected["latitude"].values)
    assert np.array_equal(result["longitude"].values, expected["longitude"].values)


def test_coarsen_reference_grid_allows_near_equal_mixed_axes(tmp_path, coarsen):
    """Lat slightly finer, lon slightly coarser — must not ping-pong to downscale."""
    import numpy as np

    src = write_zarr(
        make_gridded(
            lats=tuple(np.arange(0.0, 0.20, 0.05)),
            lons=tuple(np.arange(10.0, 10.20, 0.05)),
            fill=1.0,
        ),
        tmp_path / "src.zarr",
    )
    ref = write_zarr(
        make_gridded(
            lats=tuple(np.arange(0.0, 0.20, 0.0499)),
            lons=tuple(np.arange(10.0, 10.20, 0.0501)),
            fill=2.0,
        ),
        tmp_path / "ref.zarr",
    )
    out = tmp_path / "out.zarr"
    run_skill(coarsen, "-i", str(src), "-o", str(out), "--reference-grid", str(ref))
    assert Path(out).exists()


def test_coarsen_equal_resolution_offset_realigns(tmp_path, coarsen):
    """--target-resolution equal to input with a new --offset is a lateral shift."""
    src = write_zarr(
        make_gridded(
            lats=(0.0, 0.05, 0.10),
            lons=(10.0, 10.05, 10.10),
            fill=1.0,
        ),
        tmp_path / "src.zarr",
    )
    out = tmp_path / "out.zarr"
    run_skill(
        coarsen,
        "-i",
        str(src),
        "-o",
        str(out),
        "--target-resolution",
        "0.05",
        "--offset",
        "0.025",
    )
    ds = xr.open_zarr(out, consolidated=True)
    assert Path(out).exists()
    assert ds.sizes["longitude"] >= 1


def test_coarsen_requires_mode(tmp_path, coarsen):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(coarsen, "-i", str(src), "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def _fine_grid():
    """0.25° grid over [0, 2) x [10, 12): two 1° cells per axis, distinct box means."""
    import numpy as np

    lats = tuple(np.arange(0.125, 2.0, 0.25))
    lons = tuple(np.arange(10.125, 12.0, 0.25))
    ds = make_gridded(n_time=1, lats=lats, lons=lons)
    rng = np.random.default_rng(0)
    ds["precip"].values = rng.gamma(1.0, 5.0, ds["precip"].shape)
    return ds


def _run(coarsen, src, out, *extra):
    run_skill(
        coarsen,
        "-i",
        str(src),
        "-o",
        str(out),
        "--target-resolution",
        "1.0",
        "--offset",
        "0.5",
        *extra,
    )
    return xr.open_zarr(out)["precip"]


def test_conservative_is_box_mean_not_point_sample(tmp_path, coarsen):
    ds = _fine_grid()
    src = write_zarr(ds, tmp_path / "in.zarr")
    got = _run(coarsen, src, tmp_path / "out.zarr", "--method", "conservative")
    box = ds["precip"].sel(latitude=slice(0, 1), longitude=slice(10, 11)).mean()
    cell = got.sel(latitude=0.5, longitude=10.5).squeeze()
    assert float(cell) == pytest.approx(float(box), rel=1e-3)
    assert units_equal(got.attrs["units"], "mm day-1")


def test_conservative_missing_cell_is_nan_unless_min_fraction(tmp_path, coarsen):
    ds = _fine_grid()
    ds["precip"][0, 0, 0] = float("nan")  # one of 16 source cells in the (0.5, 10.5) box
    src = write_zarr(ds, tmp_path / "in.zarr")

    strict = _run(coarsen, src, tmp_path / "a.zarr", "--method", "conservative")
    assert bool(strict.sel(latitude=0.5, longitude=10.5).isnull().all())
    assert bool(strict.sel(latitude=1.5, longitude=11.5).notnull().all())

    partial = _run(
        coarsen, src, tmp_path / "b.zarr", "--method", "conservative", "--min-fraction", "0.9"
    )
    assert bool(partial.sel(latitude=0.5, longitude=10.5).notnull().all())


def test_min_fraction_requires_conservative(tmp_path, coarsen):
    src = write_zarr(_fine_grid(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        _run(coarsen, src, tmp_path / "out.zarr", "--min-fraction", "0.5")
    assert exc.value.code == 2


def _pipeline_input(tmp_path):
    """Input with a per-time coord, like aggregate-temporal output, and a non-grid variable."""
    ds = _fine_grid()
    ds = xr.concat([ds, ds.assign_coords(time=ds.time + np.timedelta64(1, "D"))], dim="time")
    ds = ds.assign_coords(aggregation_coverage=("time", [1.0, 1.0]))
    ds["station_count"] = ("time", [3, 4])  # a data variable with no lat/lon
    path = tmp_path / "in.zarr"
    ds.to_zarr(path, consolidated=True)
    return path


@pytest.mark.parametrize("method", ["linear", "conservative"])
def test_coarsen_passes_non_grid_variables_through(tmp_path, coarsen, method):
    src = _pipeline_input(tmp_path)
    got = _run(coarsen, src, tmp_path / "out.zarr", "--method", method)
    out = xr.open_zarr(tmp_path / "out.zarr")
    assert got.sizes == {"time": 2, "latitude": 2, "longitude": 2}
    assert out["station_count"].dims == ("time",)
    assert list(out["station_count"].values) == [3, 4]


def test_conservative_cell_past_input_edge_is_nan_unless_min_fraction(tmp_path, coarsen):
    # Input cells span lat 0..2, lon 10..12. Offset 0.25 puts the first target
    # cell at -0.25..0.75 (lat) and 9.75..10.75 (lon): 75% x 75% inside.
    src = write_zarr(_fine_grid(), tmp_path / "in.zarr")

    def run(out, *extra):
        run_skill(
            coarsen,
            "-i",
            str(src),
            "-o",
            str(out),
            "--target-resolution",
            "1.0",
            "--offset",
            "0.25",
            "--method",
            "conservative",
            *extra,
        )
        return xr.open_zarr(out)["precip"].isel(time=0)

    strict = run(tmp_path / "a.zarr")
    assert bool(strict.sel(latitude=0.25, longitude=10.25).isnull())
    assert bool(strict.sel(latitude=1.25, longitude=11.25).notnull())
    loose = run(tmp_path / "b.zarr", "--min-fraction", "0.5")
    assert bool(loose.sel(latitude=0.25, longitude=10.25).notnull())


def test_conservative_edge_cells_are_reported(tmp_path, coarsen, capsys):
    """A target cell hanging past the input's edge goes NaN, and stderr says so."""
    src = write_zarr(_fine_grid(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    # Offset 0.25: the 0.25 row and the 10.25 column are only 75% inside the input.
    run_skill(
        coarsen,
        "-i",
        str(src),
        "-o",
        str(out),
        "--target-resolution",
        "1.0",
        "--offset",
        "0.25",
        "--method",
        "conservative",
    )
    got = xr.open_zarr(out, consolidated=True)["precip"]
    assert int(got.isnull().sum()) == 3
    err = capsys.readouterr().err
    assert "3 of 4 target cells" in err
    assert "--min-fraction" in err
