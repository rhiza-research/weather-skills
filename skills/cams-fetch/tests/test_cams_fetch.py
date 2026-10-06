"""Correctness tests for cams-fetch (no network: helpers, mocked ADS + GRIB decode)."""

import json
import os
import sys
import types
from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core import UsageError

KENYA = "5.1/33.9/-4.8/41.9"


@pytest.fixture(scope="module")
def mod():
    return load_skill("cams-fetch", "fetch")


def test_resolve_variables(mod):
    assert mod._resolve_variables(None) == ["pm25"]
    assert mod._resolve_variables([["pm25", "no2"], ["pm25,o3"]]) == ["pm25", "no2", "o3"]
    assert mod._resolve_variables(["particulate_matter_10um"]) == ["pm10"]
    with pytest.raises(UsageError, match="Available"):
        mod._resolve_variables(["pm2.5"])


def test_variables_most_used_first(mod):
    assert list(mod.VARIABLES)[:4] == ["pm25", "pm10", "no2", "o3"]


def test_groups_split_gases_from_single_level(mod):
    assert mod._groups(["pm25", "aod550"]) == [(False, ["pm25", "aod550"])]
    assert mod._groups(["no2"]) == [(True, ["no2"])]
    assert mod._groups(["pm25", "no2", "o3"]) == [(False, ["pm25"]), (True, ["no2", "o3"])]


def test_build_request_forecast(mod):
    req = mod._build_request(
        "forecast",
        ["no2"],
        [5.1, 33.9, -4.8, 41.9],
        date_range=(date(2026, 10, 5), date(2026, 10, 5)),
        model_level=True,
        cycle="12",
        leads=["0", "3"],
    )
    assert req == {
        "variable": ["nitrogen_dioxide"],
        "date": ["2026-10-05/2026-10-05"],
        "area": [5.1, 33.9, -4.8, 41.9],
        "data_format": "grib",
        "type": ["forecast"],
        "time": ["12:00"],
        "leadtime_hour": ["0", "3"],
        "model_level": ["137"],
    }


def test_build_request_eac4(mod):
    req = mod._build_request(
        "eac4",
        ["pm25"],
        [5.1, 33.9, -4.8, 41.9],
        date_range=(date(2024, 1, 1), date(2024, 1, 31)),
        model_level=False,
    )
    assert req["time"] == [f"{h:02d}:00" for h in range(0, 24, 3)]
    assert "leadtime_hour" not in req and "model_level" not in req
    gas = mod._build_request(
        "eac4", ["o3"], [1, 2, 0, 3], date_range=(date(2024, 1, 1),) * 2, model_level=True
    )
    assert gas["model_level"] == ["60"]


def test_month_chunks(mod):
    assert mod._month_chunks(date(2024, 1, 20), date(2024, 3, 2)) == [
        (date(2024, 1, 20), date(2024, 1, 31)),
        (date(2024, 2, 1), date(2024, 2, 29)),
        (date(2024, 3, 1), date(2024, 3, 2)),
    ]
    assert mod._month_chunks(date(2024, 5, 3), date(2024, 5, 3)) == [(date(2024, 5, 3),) * 2]


def test_lead_hours(mod):
    assert mod._lead_hours(12, 3) == ["0", "3", "6", "9", "12"]
    with pytest.raises(UsageError):
        mod._lead_hours(200, 3)
    with pytest.raises(UsageError):
        mod._lead_hours(12, 0)


def test_split_wrapped_area(mod):
    assert mod._split_wrapped_area([10, 170, 0, -170]) == [
        [10, 170, 0, 180.0],
        [10, -180.0, 0, -170],
    ]
    assert mod._split_wrapped_area([10, 30, 0, 40]) == [[10, 30, 0, 40]]


def test_api_key_env_then_cdsapirc(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ADS_API_KEY", "envkey")
    assert mod._api_key() == "envkey"
    monkeypatch.delenv("ADS_API_KEY")
    rc = tmp_path / ".cdsapirc"
    rc.write_text("url: https://cds.climate.copernicus.eu/api\nkey: cdskey\n")
    with pytest.raises(UsageError, match="no ADS credentials"):
        mod._api_key()
    rc.write_text("url: https://ads.atmosphere.copernicus.eu/api\nkey: adskey\n")
    assert mod._api_key() == "adskey"


def test_missing_key_exits_2(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = {k: v for k, v in os.environ.items() if k != "ADS_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            run_skill(mod.fetch, "--bbox", KENYA, "--date", "2026-10-05", "-o", str(tmp_path / "o"))
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--start-time", "2026-10-01", "--end-time", "2026-10-02"],  # forecast needs --date
        ["--dataset", "eac4", "--date", "2024-01-01"],  # eac4 needs a range
        ["--date", "2014-12-31"],  # before the forecast archive
    ],
)
def test_flag_combinations_rejected(mod, tmp_path, argv):
    with pytest.raises(SystemExit) as exc:
        run_skill(mod.fetch, "--bbox", KENYA, *argv, "-o", str(tmp_path / "o"))
    assert exc.value.code == 2


def _grid():
    return {"latitude": [5.2, 4.8], "longitude": [34.0, 34.4, 34.8]}


def _cfgrib_forecast(names, init="2026-10-05"):
    """Mimic cfgrib output for a forecast GRIB: (step, lat, lon) + scalar init time."""
    steps = pd.to_timedelta([0, 3, 6], unit="h")
    coords = {**_grid(), "step": steps, "time": np.datetime64(init, "ns"), "hybrid": 137.0}
    coords["valid_time"] = ("step", np.datetime64(init, "ns") + steps.values)
    data = {
        n: (("step", "latitude", "longitude"), np.full((3, 2, 3), v, dtype="float32"))
        for n, v in names.items()
    }
    ds = xr.Dataset(data, coords=coords)
    for n in names:
        ds[n].attrs = {"GRIB_shortName": n, "GRIB_units": "kg m**-3"}
    return ds


def _cfgrib_eac4(names, start, days):
    times = pd.date_range(start, periods=8 * days, freq="3h")
    coords = {**_grid(), "time": times, "step": np.timedelta64(0, "ns"), "surface": 0.0}
    data = {
        n: (("time", "latitude", "longitude"), np.full((len(times), 2, 3), v, dtype="float32"))
        for n, v in names.items()
    }
    return xr.Dataset(data, coords=coords)


class _Remote:
    def __init__(self, req):
        self.req = req
        self.results_ready = True

    def download(self, path):
        with open(path, "w") as fh:
            json.dump(self.req, fh)


def _fake_ads(monkeypatch, mod, decode):
    submitted = []

    class Client:
        def __init__(self, url, key):
            assert url == mod._ADS_URL and key == "k"

        def submit(self, dataset_id, request):
            submitted.append((dataset_id, request))
            return _Remote(request)

    pkg = types.ModuleType("ecmwf.datastores")
    pkg.Client = Client
    monkeypatch.setitem(sys.modules, "ecmwf.datastores", pkg)
    monkeypatch.setattr(mod, "_wait", lambda remotes, label: None)
    cfgrib = types.ModuleType("cfgrib")
    cfgrib.open_datasets = lambda path: [decode(json.loads(open(path).read()))]
    monkeypatch.setitem(sys.modules, "cfgrib", cfgrib)
    monkeypatch.setenv("ADS_API_KEY", "k")
    return submitted


def test_forecast_end_to_end_units_and_shape(mod, monkeypatch, tmp_path):
    def decode(req):
        if req.get("model_level"):
            return _cfgrib_forecast({"no2": 1e-9, "go3": 5e-8})
        return _cfgrib_forecast({"pm2p5": 2.5e-8})

    submitted = _fake_ads(monkeypatch, mod, decode)
    out = tmp_path / "f.zarr"
    run_skill(
        mod.fetch, "--bbox", KENYA, "--date", "2026-10-05", "-v", "pm25", "no2", "o3",
        "--max-lead-hours", "6", "-o", str(out),
    )  # fmt: skip
    assert [s[0] for s in submitted] == ["cams-global-atmospheric-composition-forecasts"] * 2
    assert submitted[1][1]["model_level"] == ["137"]

    ds = xr.open_zarr(out)
    assert set(ds.data_vars) == {"pm25", "no2", "o3"}
    assert ds["pm25"].dims == ("step", "latitude", "longitude")
    assert ds["pm25"].attrs["units"] == "ug m-3"
    np.testing.assert_allclose(ds["pm25"].values, 25.0, rtol=1e-5)
    # kg/kg -> ppb: 1e-9 * 28.9647 / 46.0055 * 1e9
    np.testing.assert_allclose(ds["no2"].values, 28.9647 / 46.0055, rtol=1e-5)
    assert ds["no2"].attrs["units"] == "nmol mol-1"
    assert ds["o3"].attrs["standard_name"] == "mole_fraction_of_ozone_in_air"
    assert "GRIB_units" not in ds["pm25"].attrs
    assert "hybrid" not in ds.coords
    assert ds.attrs["weather_skills_source"] == "cams:cams-global-atmospheric-composition-forecasts"


def test_eac4_end_to_end_concatenates_months(mod, monkeypatch, tmp_path):
    def decode(req):
        start, end = req["date"][0].split("/")
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        return _cfgrib_eac4({"aod550": 0.3}, start, days)

    submitted = _fake_ads(monkeypatch, mod, decode)
    out = tmp_path / "r.zarr"
    run_skill(
        mod.fetch, "--bbox", KENYA, "--dataset", "eac4", "--start-time", "2024-01-31",
        "--end-time", "2024-02-01", "-v", "aod550", "-o", str(out),
    )  # fmt: skip
    assert len(submitted) == 2  # one request per month
    ds = xr.open_zarr(out)
    assert ds["aod550"].dims == ("time", "latitude", "longitude")
    assert ds.sizes["time"] == 16
    assert ds["aod550"].attrs["units"] == "1"
    assert "step" not in ds.coords
