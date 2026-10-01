"""Correctness tests for onset-date."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr


@pytest.fixture(scope="module")
def onset_date():
    return load_skill("onset-date", "onset_date").onset_date


def _forecast_ds(values, name="tp", extra_var=None):
    """A single-gridpoint forecast (`step` lead-time dim) carrying `values`
    as its daily rainfall series, for exercising the onset search directly."""
    n = len(values)
    steps = np.array([np.timedelta64(d, "D") for d in range(1, n + 1)])
    data = np.asarray(values, dtype=np.float64).reshape(n, 1, 1)
    coords = {
        "time": np.datetime64("2026-01-01", "ns"),
        "step": steps,
        "latitude": [1.0],
        "longitude": [10.0],
    }
    data_vars = {name: (["step", "latitude", "longitude"], data)}
    if extra_var is not None:
        data_vars["mask"] = (["latitude", "longitude"], np.array([[1.0]]))
    ds = xr.Dataset(data_vars, coords=coords)
    ds[name].attrs.update(units="mm", long_name="Total precipitation")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["step"].attrs.update(
        standard_name="forecast_period", long_name="time since forecast_reference_time"
    )
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    return ds


def test_icpac_onset_detected(tmp_path, onset_date):
    # Days 0-2 (3-day wet spell) sum to 25mm > 20mm threshold; no dry spell
    # (>=7 consecutive days < 1mm) within the following 21 days.
    values = [10, 10, 5] + [2.0] * 27
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "ICPAC",
        "--wet-spell-thresh",
        "20",
        "--wet-spell-days",
        "3",
        "--dry-spell-thresh",
        "1",
        "--dry-spell-days",
        "7",
        "--search-days",
        "21",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert onset == np.timedelta64(1, "D")  # onset on the first day (step index 0)
    assert ds["onset_tp_date"].attrs["standard_name"] is None
    assert "ICPAC" in ds["onset_tp_date"].attrs["long_name"]
    assert "step" not in ds.dims


def test_icpac_onset_disqualified_by_dry_spell(tmp_path, onset_date):
    # Same qualifying wet spell, but followed immediately by an 8-day dry
    # run inside the 21-day search window -> onset must not fire here.
    values = [10, 10, 5] + [0.0] * 8 + [2.0] * 19
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "ICPAC",
        "--wet-spell-thresh",
        "20",
        "--wet-spell-days",
        "3",
        "--dry-spell-thresh",
        "1",
        "--dry-spell-days",
        "7",
        "--search-days",
        "21",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert onset != np.timedelta64(1, "D")


def test_icpac_all_dry_gives_nat(tmp_path, onset_date):
    values = [0.0] * 30
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "ICPAC",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert np.isnat(onset)


def test_chc_start_grow_season_onset(tmp_path, onset_date):
    # Days 0-9 (period1=10) sum to exactly 20mm; days 10-29 (period2=20)
    # sum to more than 20mm -> onset at day 0.
    values = [2.0] * 10 + [1.5] * 20
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "CHC_start_grow_season",
        "--period1-days",
        "10",
        "--period1-thresh",
        "20",
        "--period2-days",
        "20",
        "--period2-thresh",
        "20",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert onset == np.timedelta64(1, "D")
    assert "CHC_start_grow_season" in ds["onset_tp_date"].attrs["long_name"]


def test_chc_start_grow_season_fails_period2(tmp_path, onset_date):
    # period1 satisfied at day 0, but period2 total (20mm) never exceeds
    # its own threshold anywhere in the series -> NaT.
    values = [2.0] * 10 + [0.5] * 20
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "CHC_start_grow_season",
        "--period1-days",
        "10",
        "--period1-thresh",
        "20",
        "--period2-days",
        "20",
        "--period2-thresh",
        "20",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert np.isnat(onset)


def test_variable_selection_and_passthrough(tmp_path, onset_date):
    values = [10, 10, 5] + [2.0] * 27
    src = write_zarr(_forecast_ds(values, extra_var=True), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "ICPAC",
        "--variable",
        "tp",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert "mask" in ds.data_vars
    assert ds["mask"].values[0, 0] == 1.0  # untouched passthrough


def test_nan_in_window_gives_nat(tmp_path, onset_date):
    values = [10.0, 10.0, np.nan] + [2.0] * 27
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "ICPAC",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert np.isnat(onset)


def test_requires_definition(tmp_path, onset_date):
    src = write_zarr(_forecast_ds([2.0] * 30), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(onset_date, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def test_output_variable_name_is_not_misclassified(tmp_path, onset_date):
    """Regression: a `{var}_onset_date`-style name (e.g. `tp_onset_date`)
    starts with the precip name-hint "tp", which weather_skills_core's
    variable classifier then treats as a standard "precip" kind requiring a
    `units` attr -- one this datetime/timedelta output can never carry (see
    the write-time CF-encoding note above). That silently broke reading this
    skill's own output back in as `--input` to any other skill. `onset_{var}_date`
    sandwiches `var` so it never sits at either end of the name."""
    from weather_skills_core.units import classify_variable

    src = write_zarr(_forecast_ds([10, 10, 5] + [2.0] * 27), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(onset_date, "-i", str(src), "-o", str(out), "--definition", "ICPAC")

    ds = xr.open_zarr(out, consolidated=True)
    assert "onset_tp_date" in ds.data_vars
    assert classify_variable("onset_tp_date") is None
