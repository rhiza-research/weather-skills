"""Correctness tests for cams-fetch (no network: mocked ADS + GRIB decode)."""

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

KENYA = "5.1/33.9/-4.8/41.9"


@pytest.fixture(scope="module")
def mod():
    return load_skill("cams-fetch", "fetch")


def test_variables_most_used_first(mod):
    assert list(mod.VARIABLES)[:4] == ["pm25", "pm10", "no2", "o3"]


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


class _Submitted(list):
    key = None


class _Remote:
    def __init__(self, req):
        self.req = req
        self.results_ready = True

    def download(self, path):
        with open(path, "w") as fh:
            json.dump(self.req, fh)


def _fake_ads(monkeypatch, mod, decode):
    submitted = _Submitted()

    class Client:
        def __init__(self, url, key):
            assert url == mod._ADS_URL
            submitted.key = key

        def submit(self, dataset_id, request):
            submitted.append((dataset_id, request))
            return _Remote(request)

    pkg = types.ModuleType("ecmwf.datastores")
    pkg.Client = Client
    processing = types.ModuleType("ecmwf.datastores.processing")
    processing.ProcessingFailedError = type("ProcessingFailedError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "ecmwf.datastores", pkg)
    monkeypatch.setitem(sys.modules, "ecmwf.datastores.processing", processing)
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
        mod.fetch, "--bbox", KENYA, "--date", "2026-10-05", "-v", "pm25", "-v", "no2", "-v", "o3",
        "--max-lead", "6h", "--cycle", "0", "-o", str(out),
    )  # fmt: skip
    assert [s[0] for s in submitted] == ["cams-global-atmospheric-composition-forecasts"] * 2
    assert submitted[1][1]["model_level"] == ["137"]
    assert submitted[0][1]["leadtime_hour"] == ["0", "3", "6"]
    assert submitted[0][1]["time"] == ["00:00"]

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


def test_antimeridian_box_has_one_seam_meridian(mod, monkeypatch, tmp_path):
    def decode(req):
        n, w, s, e = req["area"]
        lons = [170.0, 175.0, 180.0] if w == 170 else [-180.0, -175.0, -170.0]
        fill = 1e-8 if w == 170 else 2e-8
        ds = _cfgrib_forecast({"pm2p5": fill})
        return ds.assign_coords(longitude=lons)

    submitted = _fake_ads(monkeypatch, mod, decode)
    out = tmp_path / "wrap.zarr"
    run_skill(
        mod.fetch, "--bbox", "10/170/0/-170", "--date", "2026-10-05", "--max-lead", "6",
        "-o", str(out),
    )  # fmt: skip
    assert [s[1]["area"] for s in submitted] == [[10, 170, 0, 180.0], [10, -180.0, 0, -170]]
    ds = xr.open_zarr(out)
    lons = ds["longitude"].values.tolist()
    assert lons == [-180.0, -175.0, -170.0, 170.0, 175.0]
    # Each leg's values stay on its own longitudes.
    np.testing.assert_allclose(ds["pm25"].isel(step=0, latitude=0).values, [10, 20, 20, 10, 10])


def _pm25_forecast(req):
    return _cfgrib_forecast({"pm2p5": 1e-8})


def _run_forecast(mod, monkeypatch, tmp_path, *argv, decode=_pm25_forecast):
    submitted = _fake_ads(monkeypatch, mod, decode)
    run_skill(mod.fetch, "--bbox", KENYA, "--date", "2026-10-05", *argv, "-o", str(tmp_path / "o"))
    return submitted


def test_repeated_variable_flags(mod, monkeypatch, tmp_path):
    def decode(req):
        return _cfgrib_forecast({"pm2p5": 1e-8, "pm10": 1e-8, "aod550": 0.2})

    submitted = _run_forecast(
        mod, monkeypatch, tmp_path, "-v", "pm25", "-v", "pm10", "-v", "pm25", "-v", "aod550",
        "--max-lead", "6", decode=decode,
    )  # fmt: skip
    assert submitted[0][1]["variable"] == [
        "particulate_matter_2.5um",
        "particulate_matter_10um",
        "total_aerosol_optical_depth_550nm",
    ]
    assert "model_level" not in submitted[0][1]


def test_unknown_variable_exits_2(mod, monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _run_forecast(mod, monkeypatch, tmp_path, "-v", "pm2.5")
    assert exc.value.code == 2


def test_forecast_request(mod, monkeypatch, tmp_path):
    def decode(req):
        return _cfgrib_forecast({"no2": 1e-9})

    submitted = _run_forecast(
        mod, monkeypatch, tmp_path, "-v", "no2", "--cycle", "12", "--max-lead", "3h", decode=decode
    )
    assert submitted == [
        (
            "cams-global-atmospheric-composition-forecasts",
            {
                "data_format": "grib",
                "type": ["forecast"],
                "time": ["12:00"],
                "leadtime_hour": ["0", "3"],
                "variable": ["nitrogen_dioxide"],
                "date": ["2026-10-05/2026-10-05"],
                "area": [5.1, 33.9, -4.8, 41.9],
                "model_level": ["137"],
            },
        )
    ]


@pytest.mark.parametrize(
    ("max_lead", "leads"),
    [("6", ["0", "3", "6"]), ("6h", ["0", "3", "6"]), ("8h", ["0", "3", "6"]), ("0h", ["0"])],
)
def test_max_lead_forms(mod, monkeypatch, tmp_path, max_lead, leads):
    submitted = _run_forecast(mod, monkeypatch, tmp_path, "--max-lead", max_lead)
    assert submitted[0][1]["leadtime_hour"] == leads


@pytest.mark.parametrize(
    "argv",
    [["--max-lead", bad] for bad in ("200", "6D", "-3h", "soon")] + [["--cycle", "6"]],
)
def test_bad_leads_exit_2(mod, monkeypatch, tmp_path, argv):
    with pytest.raises(SystemExit) as exc:
        _run_forecast(mod, monkeypatch, tmp_path, *argv)
    assert exc.value.code == 2


def test_eac4_requests_per_month(mod, monkeypatch, tmp_path):
    def decode(req):
        start, end = req["date"][0].split("/")
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        return _cfgrib_eac4(
            {"go3": 1e-8} if req.get("model_level") else {"pm2p5": 1e-8}, start, days
        )

    submitted = _fake_ads(monkeypatch, mod, decode)
    run_skill(
        mod.fetch, "--bbox", KENYA, "--dataset", "eac4", "--start-time", "2024-01-20",
        "--end-time", "2024-03-02", "-v", "pm25", "-v", "o3", "-o", str(tmp_path / "o"),
    )  # fmt: skip
    dates = [req["date"][0] for _, req in submitted]
    assert dates == [
        "2024-01-20/2024-01-31", "2024-01-20/2024-01-31",
        "2024-02-01/2024-02-29", "2024-02-01/2024-02-29",
        "2024-03-01/2024-03-02", "2024-03-01/2024-03-02",
    ]  # fmt: skip
    assert submitted[0][1]["time"] == [f"{h:02d}:00" for h in range(0, 24, 3)]
    assert "leadtime_hour" not in submitted[0][1]
    assert "model_level" not in submitted[0][1]
    assert submitted[1][1]["model_level"] == ["60"]
    ds = xr.open_zarr(tmp_path / "o")
    assert ds.sizes["time"] == 8 * (12 + 29 + 2)


def test_api_key_from_cdsapirc_only_for_ads(mod, monkeypatch, tmp_path):
    submitted = _fake_ads(monkeypatch, mod, _pm25_forecast)
    monkeypatch.delenv("ADS_API_KEY")
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = tmp_path / ".cdsapirc"
    argv = ("--bbox", KENYA, "--date", "2026-10-05", "--max-lead", "6", "-o", str(tmp_path / "o"))
    rc.write_text("url: https://cds.climate.copernicus.eu/api\nkey: cdskey\n")
    with pytest.raises(SystemExit) as exc:
        run_skill(mod.fetch, *argv)
    assert exc.value.code == 2
    rc.write_text("url: https://ads.atmosphere.copernicus.eu/api\nkey: adskey\n")
    run_skill(mod.fetch, *argv)
    assert submitted.key == "adskey"
