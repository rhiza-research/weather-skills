"""Correctness tests for mrms-fetch (mocked catalog)."""

from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("mrms-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def test_resolve_precip_aliases_to_surface(mod):
    names = ["precipitation_surface", "precipitation_radar_only_surface"]
    for token in ("precip", "precipitation", "tp", "total_precipitation", "pr"):
        assert mod._resolve_variables([token], names) == ["precipitation_surface"]
    assert mod._resolve_variables(["precipitation_radar_only"], names) == [
        "precipitation_radar_only_surface"
    ]


def test_resolve_unknown_lists_available(mod):
    with pytest.raises(Exception, match="Available: temperature_2m"):
        mod._resolve_variables(["tp"], ["temperature_2m"])


def test_end_time_includes_whole_sub_daily_day(tmp_path, mod, fetch):
    times = np.arange(
        np.datetime64("2026-01-01T00:00"),
        np.datetime64("2026-01-03T01:00"),
        np.timedelta64(1, "h"),
    )
    ds = xr.Dataset(
        {"precipitation_surface": (("time", "latitude", "longitude"), np.ones((len(times), 1, 1)))},
        coords={"time": times, "latitude": [1.0], "longitude": [10.0]},
    )
    ds["precipitation_surface"].attrs["units"] = "mm day-1"
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value=ds):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
        )
    written = xr.open_zarr(out, consolidated=True)
    assert written.sizes["time"] == 48
    assert str(written["time"].values[-1])[:16] == "2026-01-02T23:00"


def test_window_past_latest_time_is_reported_not_padded(tmp_path, mod, fetch, capsys):
    # Store ends 2026-01-02T05:30; asking through 2026-01-05 must warn and write
    # only the published times.
    times = np.arange(
        np.datetime64("2026-01-01T00:00"),
        np.datetime64("2026-01-02T06:00"),
        np.timedelta64(30, "m"),
    )
    ds = xr.Dataset(
        {"precipitation_surface": (("time", "latitude", "longitude"), np.ones((len(times), 1, 1)))},
        coords={"time": times, "latitude": [1.0], "longitude": [10.0]},
    )
    ds["precipitation_surface"].attrs["units"] = "mm day-1"
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value=ds):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-05",
            "-o",
            str(out),
        )
    err = capsys.readouterr().err
    assert "covers only 2026-01-01T00:00..2026-01-02T05:30" in err
    assert xr.open_zarr(out, consolidated=True).sizes["time"] == len(times)


def test_complete_window_has_no_partial_warning(tmp_path, mod, fetch, capsys):
    times = np.arange(
        np.datetime64("2026-01-01"), np.datetime64("2026-01-04"), np.timedelta64(1, "D")
    )
    ds = xr.Dataset(
        {"precipitation_surface": (("time", "latitude", "longitude"), np.ones((3, 1, 1)))},
        coords={"time": times, "latitude": [1.0], "longitude": [10.0]},
    )
    ds["precipitation_surface"].attrs["units"] = "mm day-1"
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value=ds):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "-o",
            str(out),
        )
    assert "covers only" not in capsys.readouterr().err


def test_bbox_applied_after_time_selection(tmp_path, mod, fetch):
    # An antimeridian bbox concatenates two longitude wings, which reads every
    # value it is given: it must only ever see the requested window.
    times = np.arange(
        np.datetime64("2026-01-01"), np.datetime64("2026-03-01"), np.timedelta64(1, "D")
    )
    ds = xr.Dataset(
        {"temperature_2m": (("time", "latitude", "longitude"), np.ones((len(times), 1, 4)))},
        coords={"time": times, "latitude": [1.0], "longitude": [-175.0, -10.0, 10.0, 175.0]},
    )
    seen = []
    real = mod.bbox_subset

    def _spy(d, *a, **k):
        seen.append(d.sizes["time"])
        return real(d, *a, **k)

    out = tmp_path / "out.zarr"
    with (
        patch.object(mod, "_open_dataset", return_value=ds),
        patch.object(mod, "bbox_subset", _spy),
    ):
        run_skill(
            fetch,
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-02",
            "--bbox",
            "5/170/-5/-170",
            "-o",
            str(out),
        )
    assert seen == [2]
    written = xr.open_zarr(out, consolidated=True)
    assert sorted(written["longitude"].values.tolist()) == [-175.0, 175.0]


def test_unknown_variable_points_to_list_variables(mod):
    with pytest.raises(Exception) as exc:
        mod._resolve_variables(["list_vars_please"], ["tp"])
    assert "--list-variables" in str(exc.value)


def test_analysis_repeated_windows_take_each_window(tmp_path, mod, fetch):
    times = np.arange(
        np.datetime64("2024-12-30"), np.datetime64("2026-01-03"), np.timedelta64(1, "D")
    ).astype("datetime64[ns]")
    ds = xr.Dataset(
        {
            "precipitation_surface": (
                ("time", "latitude", "longitude"),
                np.ones((times.size, 2, 2)),
                {"units": "mm day-1"},
            )
        },
        coords={"time": times, "latitude": [1.0, 2.0], "longitude": [10.0, 11.0]},
    )
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value=ds):
        run_skill(
            fetch,
            "--start-time",
            "2025-01-01",
            "--end-time",
            "2025-01-02",
            "--start-time",
            "2026-01-01",
            "--end-time",
            "2026-01-05",
            "-o",
            str(out),
        )
    days = xr.open_zarr(out)["time"].values.astype("datetime64[D]").astype(str).tolist()
    # The second window runs past the data: kept as far as it goes, not padded.
    assert days == ["2025-01-01", "2025-01-02", "2026-01-01", "2026-01-02"]


def _hourly_ds(end="2026-01-02T06:00"):
    times = np.arange(
        np.datetime64("2026-01-01T00:00"), np.datetime64(end), np.timedelta64(1, "h")
    ).astype("datetime64[ns]")
    shape = (times.size, 2, 2)
    return xr.Dataset(
        {
            "precipitation_surface": (
                ("time", "latitude", "longitude"),
                np.full(shape, 1e-4),
                {"units": "kg m-2 s-1"},
            ),
            "categorical_precipitation_type_surface": (
                ("time", "latitude", "longitude"),
                np.ones(shape),
                {"units": "1"},
            ),
        },
        coords={"time": times, "latitude": [30.0, 31.0], "longitude": [-100.0, -99.0]},
    )


def test_precip_written_as_precip(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_dataset", return_value=_hourly_ds()):
        run_skill(fetch, "--start-time", "2026-01-01", "--end-time", "2026-01-01", "-o", str(out))
    written = xr.open_zarr(out, consolidated=True)
    assert set(written.data_vars) == {"precip", "categorical_precipitation_type_surface"}
    assert written["precip"].attrs["units"] == "mm day-1"
    assert written["precip"].attrs["data_interval"] == "1 hour"
    np.testing.assert_allclose(written["precip"].values, 1e-4 * 86400)
    assert written["categorical_precipitation_type_surface"].attrs["units"] == "1"
    assert written.attrs["weather_skills_source"] == "dynamical:noaa-mrms-conus-analysis-hourly"
    assert load_history(out)[-1]["skill"] == "mrms-fetch"


def test_missing_window_exits_2(tmp_path, mod, fetch):
    with patch.object(mod, "_open_dataset", return_value=_hourly_ds()):
        with pytest.raises(SystemExit) as exc:
            run_skill(fetch, "-o", str(tmp_path / "out.zarr"))
    assert exc.value.code == 2


def test_probe_latest_notes_partial_day(mod, fetch, capsys):
    with patch.object(mod, "_open_dataset", return_value=_hourly_ds()):
        run_skill(fetch, "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-01-02"
    assert "2026-01-02T05:00; that day is partial" in captured.err


def test_list_variables_prints_fields(mod, fetch, capsys):
    with patch.object(mod, "_open_dataset", return_value=_hourly_ds()):
        run_skill(fetch, "--list-variables")
    assert "  precipitation_surface\tkg m-2 s-1\t" in capsys.readouterr().out
