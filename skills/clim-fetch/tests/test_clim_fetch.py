"""Correctness tests for clim-fetch (mocked GCS)."""

import io
import json
from unittest.mock import patch

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("clim-fetch", "fetch")


def test_missing_pair_lists_available_climatologies(tmp_path, mod, capsys):
    listing = {
        "prefixes": [
            "climatologies/era5_precip_1d.zarr/",
            "climatologies/gefs_tmp2m_1d.zarr/",
            "climatologies/imerg_final_precip_1d.zarr/",
        ]
    }

    def _urlopen(url, timeout=None):
        return io.BytesIO(json.dumps(listing).encode())

    with (
        patch.object(mod, "_open_zarr_or_none", return_value=None),
        patch("urllib.request.urlopen", _urlopen),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(
            mod.fetch,
            "--dataset",
            "era5",
            "--variable",
            "tmp2m",
            "--start-time",
            "2026-09-01",
            "--end-time",
            "2026-09-02",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "no climatology for --dataset era5 --variable tmp2m" in err
    assert "--dataset gefs --variable tmp2m" in err
    assert "--dataset imerg_final --variable precip" in err


@pytest.mark.parametrize(
    "start, end",
    [("2021-02-27", "2021-03-02"), ("2020-02-28", "2020-03-01"), ("2021-12-30", "2022-01-01")],
)
def test_each_date_gets_its_own_calendar_days_climatology(mod, start, end):
    """Match by month/day in the 1904 cycle: a non-leap Mar 1 is not 1904's Feb 29."""
    import numpy as np
    import pandas as pd
    import xarray as xr

    cycle = pd.date_range("1904-01-01", "1904-12-31", freq="D")
    clim = xr.Dataset(
        {"precip_avg": ("time", (cycle.month * 100 + cycle.day).to_numpy(dtype=float))},
        coords={"time": cycle},
    )
    out = mod._expand_climatology(clim, pd.Timestamp(start), pd.Timestamp(end))
    dates = pd.DatetimeIndex(out["time"].values)
    assert out["precip_avg"].values.tolist() == (dates.month * 100 + dates.day).tolist()
    assert np.isfinite(out["precip_avg"].values).all()


def test_lead_with_no_source_value_past_dec31_warns_and_stays_nan(tmp_path, mod, capsys):
    """At lead N the mirrors hold NaN for the last N inits (valid date past Dec 31),
    so valid Jan 1..Jan N have no climatology: they stay NaN and are reported."""
    import numpy as np
    import pandas as pd
    import xarray as xr

    inits = pd.date_range("1904-01-01", "1904-12-31", freq="D")
    leads = np.array([0, 7], dtype="timedelta64[D]").astype("timedelta64[ns]")
    values = np.ones((len(inits), 2, 2, 2), dtype="float32")
    values[-7:, 1] = np.nan  # ecmwf_ifs-style: lead 7 empty for Dec 25..31 inits
    source = xr.Dataset(
        {
            "avg": (("init_time", "prediction_timedelta", "lat", "lon"), values),
            "std": (("init_time", "prediction_timedelta", "lat", "lon"), values),
        },
        coords={
            "init_time": inits,
            "prediction_timedelta": leads,
            "lat": [0.0, 1.5],
            "lon": [36.0, 37.5],
        },
        attrs={"variable": "precip", "units": "mm / day"},
    )
    out_path = tmp_path / "out.zarr"
    with patch.object(mod, "_open_zarr_or_none", return_value=source):
        run_skill(
            mod.fetch,
            "--dataset",
            "ecmwf_ifs",
            "--prediction-timedelta",
            "7",
            "--start-time",
            "2025-12-30",
            "--end-time",
            "2026-01-09",
            "-o",
            str(out_path),
        )
    out = xr.open_zarr(out_path)
    empty = out["precip_avg"].isnull().all(["latitude", "longitude"]).values
    assert pd.DatetimeIndex(out["time"].values[empty]).strftime("%m-%d").tolist() == [
        f"01-0{d}" for d in range(1, 8)
    ]
    assert np.isfinite(out["precip_avg"].isel(time=~empty).values).all()
    err = capsys.readouterr().err
    assert "7 of 11 dates have no climatology value" in err
    assert "2026-01-01" in err and "2026-01-07" in err and "2026-01-08" not in err


def _spread_source(std=1.0):
    import numpy as np
    import pandas as pd
    import xarray as xr

    dims = ("init_time", "prediction_timedelta", "lat", "lon")
    return xr.Dataset(
        {
            "avg": (dims, np.full((366, 1, 1, 1), 4.0)),
            "std": (dims, np.full((366, 1, 1, 1), std)),
        },
        coords={
            "init_time": pd.date_range("1904-01-01", periods=366),
            "prediction_timedelta": np.array([0], dtype="timedelta64[ns]"),
            "lat": [0.0],
            "lon": [36.0],
        },
        attrs={"variable": "precip", "units": "mm / day"},
    )


def _fetch_spread(mod, path, source, window, *extra, exact=False):
    def _open(key):
        return source if key.endswith("_1d.zarr") or exact else None

    with patch.object(mod, "_open_zarr_or_none", side_effect=_open):
        run_skill(
            mod.fetch,
            "--dataset",
            "chirps",
            "--start-time",
            "2026-06-01",
            "--end-time",
            "2026-06-03",
            "--window",
            str(window),
            "-o",
            str(path),
            *extra,
        )


def test_missing_window_spread_refused_by_default(tmp_path, mod, capsys):
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        _fetch_spread(mod, out, _spread_source(), 7)
    assert exc.value.code != 0
    assert not out.exists()
    err = capsys.readouterr().err
    assert "covariance" in err
    assert "--approximate-spread upper-bound" in err


@pytest.mark.parametrize("align", ["left", "right", "center"])
def test_correlated_years_use_explicit_upper_bound(tmp_path, mod, align):
    import numpy as np
    import xarray as xr

    # Each year's seven days move together: true window std equals daily std.
    years = np.repeat(np.array([[3.0], [5.0]]), 7, axis=1)
    true_std = years.mean(axis=1).std()
    out = tmp_path / "out.zarr"
    _fetch_spread(
        mod,
        out,
        _spread_source(years[:, 0].std()),
        7,
        "--approximate-spread",
        "upper-bound",
        "--align",
        align,
    )
    result = xr.open_zarr(out, consolidated=True)
    np.testing.assert_allclose(result["precip_std"].values, true_std)
    np.testing.assert_allclose(result["precip_avg"].values, years.mean())
    assert result["precip_std"].attrs["spread_is_approximate"] is True
    assert result["precip_std"].attrs["spread_method"] == "upper_bound_perfect_correlation"
    assert result.attrs["climatology_spread_is_approximate"] is True


@pytest.mark.parametrize("window", [1, 7])
def test_exact_cache_spread_is_preserved(tmp_path, mod, window):
    import numpy as np
    import xarray as xr

    out = tmp_path / "out.zarr"
    _fetch_spread(mod, out, _spread_source(0.75), window, exact=True)
    result = xr.open_zarr(out, consolidated=True)
    np.testing.assert_allclose(result["precip_std"].values, 0.75)
    assert result["precip_std"].attrs["spread_is_approximate"] is False


@pytest.mark.parametrize("std", [0.0, -1.0, float("nan")])
def test_upper_bound_preserves_zero_and_masks_invalid_spread(tmp_path, mod, std):
    import numpy as np
    import xarray as xr

    out = tmp_path / "out.zarr"
    _fetch_spread(mod, out, _spread_source(std), 7, "--approximate-spread", "upper-bound")
    got = xr.open_zarr(out, consolidated=True)["precip_std"].values
    if std == 0:
        assert (got == 0).all()
    else:
        assert np.isnan(got).all()
