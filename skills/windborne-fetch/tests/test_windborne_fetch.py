"""Correctness tests for windborne-fetch (mocked WindBorne API)."""

import numpy as np
import pytest
from conftest import load_skill, run_skill

LATS = np.array([2.0, 1.75, 1.5, 1.25, 1.0], dtype="float32")
LONS = np.array([36.0, 36.25, 36.5], dtype="float32")


@pytest.fixture(scope="module")
def mod():
    return load_skill("windborne-fetch", "fetch")


def _zipped_zarr(tmp_path, lead, arrays):
    """Build a gridded response like the API's: a zipped Zarr v3 group."""
    import zarr

    path = tmp_path / f"resp_{lead}_{len(list(tmp_path.iterdir()))}.zip"
    store = zarr.storage.ZipStore(path, mode="w")
    root = zarr.open_group(
        store,
        mode="w",
        attributes={
            "forecast_hour": lead,
            "forecast_zero": "2026-10-19T18:00:00Z",
            "initialization_time": "2026-10-19T20:00:00Z",
        },
    )
    root.create_array("latitude", data=LATS)
    root.create_array("longitude", data=LONS)
    for key, (values, units) in arrays.items():
        root.create_array(key, data=values, attributes={"units": units})
    store.close()
    return path.read_bytes()


class _Resp:
    def __init__(self, status, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.text = str(payload)

    def json(self):
        return self._payload


def _fake_api(monkeypatch, mod, tmp_path, gridded):
    """Patch requests.Session so every endpoint answers from local fixtures."""
    import requests

    calls = []

    class _Session:
        auth = None

        def get(self, url, params=None, timeout=None):
            calls.append((url.rsplit("/", 1)[-1], dict(params or {})))
            if url.endswith("/initialization_times"):
                return _Resp(200, {"available": ["2026-10-19T00:00:00Z", "2026-10-19T20:00:00Z"]})
            if url.endswith("/initialization_times/archive"):
                # Archive pages run backwards from page_end; 28 Sep has two runs.
                return _Resp(
                    200,
                    {
                        "archived_initialization_times": [
                            "2026-09-29T00:00:00.000Z",
                            "2026-09-28T12:00:00.000Z",
                            "2026-09-28T00:00:00.000Z",
                            "2026-09-27T23:00:00.000Z",
                        ]
                    },
                )
            if url.endswith("/variables"):
                return _Resp(
                    200,
                    {
                        "sfc_variables": ["total_precipitation_3h", "temperature_2m", "cape"],
                        "derived_sfc_variables": [],
                        "upper_variables": ["temperature"],
                        "levels": [500, 850],
                    },
                )
            lead = params["forecast_hour"]
            return _Resp(200, content=_zipped_zarr(tmp_path, lead, gridded(params)))

    monkeypatch.setattr(requests, "Session", _Session)
    monkeypatch.setenv("WB_CLIENT_ID", "id")
    monkeypatch.setenv("WB_API_KEY", "key")
    return calls


def test_tp_becomes_rate_at_interval_start_from_forecast_zero(mod, monkeypatch, tmp_path):
    import xarray as xr

    def gridded(params):
        lead = params["forecast_hour"]
        return {"total_precipitation_3h": (np.full((5, 3), lead / 3, "float32"), "mm")}

    calls = _fake_api(monkeypatch, mod, tmp_path, gridded)
    out = tmp_path / "wm6.zarr"
    run_skill(mod.fetch, "--bbox", "1.6/36.1/0.9/36.6", "--max-lead", "9", "-o", str(out))
    ds = xr.open_zarr(out)

    # Latest run is picked; leads come from forecast zero, not the 20Z init.
    assert calls[-1][1]["initialization_time"] == "2026-10-19T20:00:00Z"
    assert ds["time"].values == np.datetime64("2026-10-19T18:00")
    assert ds.attrs["windborne_initialization_time"] == "2026-10-19T20:00:00Z"
    # Lead h holds (h-3, h]: lead 0 dropped, others relabelled to h-3, mm/3h -> mm/day.
    assert list(ds["step"].values.astype("timedelta64[h]").astype(int)) == [0, 3, 6]
    np.testing.assert_allclose(ds["tp"].isel(latitude=0, longitude=0).values, [8.0, 16.0, 24.0])
    assert ds["tp"].attrs["units"] == "mm day-1"
    # bbox window, latitude ascending.
    assert list(ds["latitude"].values) == [1.0, 1.25, 1.5]
    assert list(ds["longitude"].values) == [36.25, 36.5]


def test_tp_shifts_but_t2m_keeps_its_lead(mod, monkeypatch, tmp_path):
    import xarray as xr

    def gridded(params):
        lead = params["forecast_hour"]
        if params["variable"] == "total_precipitation_3h":
            return {"total_precipitation_3h": (np.full((5, 3), lead / 3, "float32"), "mm")}
        return {"temperature_2m": (np.full((5, 3), 273.15 + lead, "float32"), "K")}

    _fake_api(monkeypatch, mod, tmp_path, gridded)
    out = tmp_path / "wm6.zarr"
    run_skill(mod.fetch, "-v", "tp", "-v", "t2m", "--max-lead", "6h", "-o", str(out))
    ds = xr.open_zarr(out)
    assert list(ds["step"].values.astype("timedelta64[h]").astype(int)) == [0, 3, 6]
    # t2m is instantaneous: lead h holds the value at forecast zero + h.
    np.testing.assert_allclose(ds["t2m"].isel(latitude=0, longitude=0).values, [0.0, 3.0, 6.0])
    # tp at lead h covers (h-3, h]: relabelled to h-3; nothing covers [6 h, 9 h).
    np.testing.assert_allclose(ds["tp"].isel(latitude=0, longitude=0).values, [8.0, 16.0, np.nan])


def test_max_lead_zero_with_tp_errors(mod, monkeypatch, tmp_path, capsys):
    _fake_api(monkeypatch, mod, tmp_path, lambda p: {})
    with pytest.raises(SystemExit) as exc:
        run_skill(mod.fetch, "-v", "tp", "--max-lead", "0", "-o", str(tmp_path / "x.zarr"))
    assert exc.value.code == 2
    assert "no value at lead 0" in capsys.readouterr().err
    assert not (tmp_path / "x.zarr").exists()


def test_max_lead_timedelta_and_level_without_upper_field(mod, monkeypatch, tmp_path):
    def gridded(params):
        return {"temperature_2m": (np.full((5, 3), 300.0, "float32"), "K")}

    calls = _fake_api(monkeypatch, mod, tmp_path, gridded)
    run_skill(mod.fetch, "-v", "t2m", "--max-lead", "1D", "-o", str(tmp_path / "a.zarr"))
    assert max(c[1]["forecast_hour"] for c in calls if c[0] == "gridded") == 24
    with pytest.raises(SystemExit) as exc:
        run_skill(mod.fetch, "-v", "t2m", "--level", "500", "-o", str(tmp_path / "b.zarr"))
    assert exc.value.code == 2


def test_members_and_levels(mod, monkeypatch, tmp_path):
    import xarray as xr

    def gridded(params):
        assert params["include_members"] == "true"
        return {
            "temperature_850": (np.full((5, 3), 290.0, "float32"), "K"),
            "members/temperature_850": (np.full((4, 5, 3), 290.0, "float32"), "K"),
        }

    _fake_api(monkeypatch, mod, tmp_path, gridded)
    out = tmp_path / "wm6.zarr"
    run_skill(
        mod.fetch,
        "--date",
        "2026-10-19",
        "--run",
        "00",
        "-v",
        "t",
        "--level",
        "850",
        "--ensemble",
        "members",
        "--max-lead",
        "3",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out)
    assert ds["t"].dims == ("step", "vertical", "number", "latitude", "longitude")
    assert ds["t"].attrs["units"] == "degree_Celsius"
    assert float(ds["t"].max()) == pytest.approx(16.85)


def test_std_keeps_kelvin(mod, monkeypatch, tmp_path):
    import xarray as xr

    def gridded(params):
        return {
            "temperature_2m": (np.full((5, 3), 300.0, "float32"), "K"),
            "ensemble_std/temperature_2m": (np.full((5, 3), 1.5, "float32"), "K"),
        }

    _fake_api(monkeypatch, mod, tmp_path, gridded)
    out = tmp_path / "wm6.zarr"
    run_skill(mod.fetch, "-v", "t2m", "--ensemble", "std", "--max-lead", "3", "-o", str(out))
    ds = xr.open_zarr(out)
    assert ds["t2m"].attrs["units"] == "K"
    assert float(ds["t2m"].max()) == pytest.approx(1.5)


def test_missing_members_flavour_errors(mod, monkeypatch, tmp_path):
    from weather_skills_core import DataError

    _fake_api(
        monkeypatch, mod, tmp_path, lambda p: {"cape": (np.zeros((5, 3), "float32"), "J kg-1")}
    )
    with pytest.raises((DataError, SystemExit)):
        run_skill(mod.fetch, "-v", "cape", "--ensemble", "members", "-o", str(tmp_path / "x.zarr"))
    assert not (tmp_path / "x.zarr").exists()


def test_archived_date_picks_latest_run_that_day(mod, monkeypatch, tmp_path):
    def gridded(params):
        return {"temperature_2m": (np.full((5, 3), 300.0, "float32"), "K")}

    calls = _fake_api(monkeypatch, mod, tmp_path, gridded)
    run_skill(
        mod.fetch,
        "--date",
        "2026-09-28",
        "-v",
        "t2m",
        "--max-lead",
        "0",
        "-o",
        str(tmp_path / "x.zarr"),
    )
    archive = [c for c in calls if c[0] == "archive"]
    assert archive[0][1] == {"page_end": "2026-09-29T00:00:00Z"}
    assert calls[-1][1]["initialization_time"] == "2026-09-28T12:00:00Z"


def test_date_with_no_run_errors(mod, monkeypatch, tmp_path, capsys):
    _fake_api(monkeypatch, mod, tmp_path, lambda p: {})
    with pytest.raises(SystemExit):
        run_skill(mod.fetch, "--date", "2026-09-01", "-o", str(tmp_path / "x.zarr"))
    assert "live or archived" in capsys.readouterr().err


def test_probe_latest(mod, monkeypatch, tmp_path, capsys):
    _fake_api(monkeypatch, mod, tmp_path, lambda p: {})
    run_skill(mod.fetch, "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-10-19"
    assert "--date 2026-10-19 --run 20" in captured.err


def test_missing_credentials_exit(mod, monkeypatch, tmp_path):
    monkeypatch.delenv("WB_CLIENT_ID", raising=False)
    monkeypatch.delenv("WB_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_skill(mod.fetch, "--probe-latest")
    assert exc.value.code == 2


def test_flag_for_another_product_errors(mod, monkeypatch, tmp_path, capsys):
    _fake_api(monkeypatch, mod, tmp_path, lambda p: {})
    with pytest.raises(SystemExit) as exc:
        run_skill(mod.fetch, "--point", "-1,36", "--period", "hourly", "-o", str(tmp_path / "x"))
    assert exc.value.code == 2
    assert "--product grid does not take --point, --period" in capsys.readouterr().err


def test_point_product_needs_point(mod, monkeypatch, tmp_path, capsys):
    _fake_api(monkeypatch, mod, tmp_path, lambda p: {})
    with pytest.raises(SystemExit) as exc:
        run_skill(mod.fetch, "--product", "sounding", "-o", str(tmp_path / "x"))
    assert exc.value.code == 2
    assert "needs --point" in capsys.readouterr().err
