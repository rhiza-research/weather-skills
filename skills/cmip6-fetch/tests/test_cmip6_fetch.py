"""Correctness tests for cmip6-fetch (mocked catalog + zarr open)."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill
from weather_skills_core import UsageError
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def mod():
    return load_skill("cmip6-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


def _cmip6_state():
    ds = make_gridded(n_time=2, start="2020-01-01", name="tas", fill=280.0)
    ds = ds.rename({"latitude": "lat", "longitude": "lon"})
    ds["tas"].attrs.update(
        units="K",
        long_name="Near-Surface Air Temperature",
        standard_name="air_temperature",
    )
    ds["time"].encoding.update(calendar="proleptic_gregorian", units="days since 1850-01-01")
    return {
        "ds": ds,
        "grid_label": "gn",
        "version": "v20200101",
        "source_calendar": "proleptic_gregorian",
        "source_time_units": "days since 1850-01-01",
    }


def test_fetch_writes_zarr_with_mocked_remote(tmp_path, mod, fetch):
    out = tmp_path / "out.zarr"

    with patch.object(mod, "_open_remote", return_value=_cmip6_state()):
        run_skill(
            fetch,
            "--start-time",
            "2020-01-01",
            "--end-time",
            "2020-01-02",
            "-v",
            "tas",
            "--model",
            "GFDL-CM4",
            "--experiment",
            "historical",
            "-o",
            str(out),
        )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "tas" in ds
    assert ds["tas"].attrs["units"] == "degree_Celsius"
    np.testing.assert_allclose(ds["tas"].values, 280.0 - 273.15, rtol=1e-5)
    assert ds["tas"].attrs.get("data_interval") == "1 day"
    assert load_history(out)[-1]["skill"] == "cmip6-fetch"


def test_resolve_zstore_unknown_model(mod):
    import pandas as pd

    catalog = pd.DataFrame(
        {
            "source_id": ["GFDL-CM4"],
            "experiment_id": ["historical"],
            "variable_id": ["tas"],
            "member_id": ["r1i1p1f1"],
            "table_id": ["Amon"],
            "grid_label": ["gn"],
            "version": ["v20200101"],
            "zstore": ["gs://bucket/tas.zarr"],
        }
    )
    with patch.object(mod.pd, "read_csv", return_value=catalog):
        with pytest.raises(UsageError, match="NO-SUCH-MODEL"):
            mod._resolve_zstore("NO-SUCH-MODEL", "historical", "tas", "r1i1p1f1", "Amon", None)


def test_probe_latest_is_none(capsys, fetch):
    run_skill(fetch, "--probe-latest")
    assert capsys.readouterr().out.strip() == "none"


def test_monthly_keeps_source_time_bounds(tmp_path, mod, fetch):
    """Mid-month Amon labels keep CMIP6's [1st, next 1st) edges, not inferred ones."""
    import cftime

    labels = [cftime.DatetimeNoLeap(2030, m, 15, 12) for m in (1, 2, 3)]
    edges = [cftime.DatetimeNoLeap(2030, m, 1) for m in (1, 2, 3, 4)]
    ds = make_gridded(n_time=3, name="pr", fill=1.0).rename({"latitude": "lat", "longitude": "lon"})
    ds = ds.assign_coords(time=labels)
    ds["pr"].attrs.update(units="kg m-2 s-1", standard_name="precipitation_flux")
    ds["time"].attrs["bounds"] = "time_bnds"
    ds["time_bnds"] = (("time", "bnds"), np.array([edges[:-1], edges[1:]]).T)
    state = {**_cmip6_state(), "ds": ds, "source_calendar": "noleap"}
    state["source_time_units"] = "days since 1850-01-01"
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_remote", return_value=state):
        run_skill(
            fetch,
            "--start-time",
            "2030-01-01",
            "--end-time",
            "2030-03-31",
            "-v",
            "pr",
            "--model",
            "GFDL-ESM4",
            "--experiment",
            "ssp245",
            "-o",
            str(out),
        )
    got = xr.open_zarr(out, decode_times=xr.coders.CFDatetimeCoder(use_cftime=True))
    bounds = got[got["time"].attrs["bounds"]].values
    assert [b.month for b in bounds[:, 0]] == [1, 2, 3]
    assert all(b.day == 1 for b in bounds.ravel())


def _monthly_state(calendar, year, months, name="tas"):
    """A mocked monthly store in ``calendar`` with CMIP6-style [1st, next 1st) bounds."""
    import cftime

    labels = [cftime.datetime(year, m, 16, calendar=calendar) for m in months]
    edges = [
        cftime.datetime(year + m // 13, (m - 1) % 12 + 1, 1, calendar=calendar) for m in months
    ]
    edges.append(
        cftime.datetime(year + (months[-1] + 1) // 13, months[-1] % 12 + 1, 1, calendar=calendar)
    )
    ds = make_gridded(n_time=len(months), name=name, fill=280.0)
    ds = ds.rename({"latitude": "lat", "longitude": "lon"}).assign_coords(time=labels)
    ds[name].attrs.update(units="K", standard_name="air_temperature")
    ds["time"].attrs["bounds"] = "time_bnds"
    ds["time_bnds"] = (("time", "bnds"), np.array([edges[:-1], edges[1:]]).T)
    return {**_cmip6_state(), "ds": ds, "source_calendar": calendar}


def _run(fetch, out, start, end, experiment="ssp245"):
    run_skill(
        fetch,
        "--start-time",
        start,
        "--end-time",
        end,
        "-v",
        "tas",
        "--model",
        "UKESM1-0-LL",
        "--experiment",
        experiment,
        "-o",
        str(out),
    )


def test_360_day_end_on_day_31(tmp_path, mod, fetch):
    """--end-time 2015-03-31 does not exist in a 360_day calendar; it must still select."""
    out = tmp_path / "out.zarr"
    with patch.object(mod, "_open_remote", return_value=_monthly_state("360_day", 2015, [1, 2, 3])):
        _run(fetch, out, "2015-01-01", "2015-03-31")
    got = xr.open_zarr(out, decode_times=xr.coders.CFDatetimeCoder(use_cftime=True))
    assert [t.month for t in got["time"].values] == [1, 2, 3]
    assert got["time"].values[0].calendar == "360_day"


def test_window_past_run_end_is_refused(tmp_path, capsys, mod, fetch):
    """A window crossing historical -> scenario is refused, not silently truncated."""
    out = tmp_path / "out.zarr"
    state = _monthly_state("noleap", 2014, [10, 11, 12])
    with patch.object(mod, "_open_remote", return_value=state):
        with pytest.raises(SystemExit):
            _run(fetch, out, "2014-10-01", "2015-02-28", experiment="historical")
    assert "up to (not including) 2015-01-01" in capsys.readouterr().err
    assert not out.exists()


def test_no_match_hint_lists_options_for_the_other_facets(mod):
    """A wrong --member still shows `historical` and the valid members in full."""
    import pandas as pd

    experiments = [f"a{i:02d}" for i in range(15)] + ["historical"]
    catalog = pd.DataFrame(
        {
            "source_id": "GFDL-ESM4",
            "experiment_id": experiments,
            "variable_id": "pr",
            "member_id": "r1i1p1f1",
            "table_id": "Amon",
            "grid_label": "gr1",
            "version": "v20190726",
            "zstore": "gs://bucket/pr.zarr",
        }
    )
    with patch.object(mod.pd, "read_csv", return_value=catalog):
        with pytest.raises(UsageError) as err:
            mod._resolve_zstore("GFDL-ESM4", "historical", "pr", "r9i9p9f9", "Amon", None)
    msg = str(err.value)
    assert "'historical'" in msg
    assert "members for historical/pr/Amon=['r1i1p1f1']" in msg
