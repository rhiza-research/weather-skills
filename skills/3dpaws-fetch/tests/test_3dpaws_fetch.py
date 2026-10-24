"""Correctness tests for 3dpaws-fetch (missing creds + mocked CHORDS API)."""

import os
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill

PORTAL = "https://3d-fewsnet.icdp.ucar.edu"
CREDS = {"CHORDS_EMAIL": "user@example.org", "CHORDS_API_KEY": "key"}


@pytest.fixture(scope="module")
def mod():
    return load_skill("3dpaws-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


INSTRUMENTS = [
    {"id": 1, "name": "Nairobi ", "site_id": 10},
    {"id": 2, "name": "Far south", "site_id": 11},
    {"id": 3, "name": "No coords", "site_id": 12},
]
SITES = [
    {"id": 10, "name": " KMD HQ", "lat": "-1.3", "lon": "36.76"},
    {"id": 11, "name": "South", "lat": "-10.0", "lon": "36.0"},
    {"id": 12, "name": "KSA", "lat": None, "lon": None},
]


def _readings(start, days, interval_min, make):
    """CHORDS data points every ``interval_min`` from ``start`` for ``days`` days."""
    times = pd.date_range(start, periods=days * 1440 // interval_min, freq=f"{interval_min}min")
    return [
        {"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "test": "false", "measurements": make(t)}
        for t in times
    ]


def _free_running(start, days, make, pad_hours=0):
    """1-minute readings on a station's own timer: 60-61 s apart, seconds drifting.

    ``pad_hours`` adds readings either side, as a station that keeps reporting sends.
    """
    pad = pd.Timedelta(hours=pad_hours)
    lo = pd.Timestamp(start) - pad
    hi = pd.Timestamp(start) + pd.Timedelta(days=days) + pad
    times = pd.date_range(lo, hi, freq="60400ms", inclusive="left").floor("s")
    return [
        {"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "test": "false", "measurements": make(t)}
        for t in times
    ]


def _dry(t):
    return {"rg1": 0.0, "rg2": 0.0, "mt1": 20.0}


def _fake_get_json(points_by_station):
    """Serve instruments/sites listings and the per-station data in the requested window."""

    def fake(url, params=None):
        if url.endswith("/instruments.json"):
            return INSTRUMENTS
        if url.endswith("/sites.json"):
            return SITES
        sid = url.rsplit("/", 1)[-1].removesuffix(".json")
        if "last" in params:
            data = points_by_station.get(sid, [])[-1:]
            return {"type": "FeatureCollection", "features": [{"properties": {"data": data}}]}
        lo = pd.Timestamp(params["start"]).tz_convert(None)
        hi = pd.Timestamp(params["end"]).tz_convert(None)
        data = [
            p
            for p in points_by_station.get(sid, [])
            if lo <= pd.Timestamp(p["time"]).tz_convert(None) < hi
        ]
        return {"type": "FeatureCollection", "features": [{"properties": {"data": data}}]}

    return fake


def _run(tmp_path, mod, fetch, points_by_station, *extra):
    out = tmp_path / "out.zarr"
    stations = [arg for sid in points_by_station for arg in ("--station", sid)]
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod, "_get_json", side_effect=_fake_get_json(points_by_station)),
    ):
        run_skill(
            fetch,
            *stations,
            "--start-time",
            "2026-10-01",
            "--end-time",
            "2026-10-02",
            *extra,
            "-o",
            str(out),
        )
    return xr.open_zarr(out)


def test_missing_credentials_exits_2(tmp_path, mod, fetch):
    env = {k: v for k, v in os.environ.items() if k not in CREDS}
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(mod, "_get_json", side_effect=_fake_get_json({})),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(
            fetch,
            "--station",
            "1",
            "--start-time",
            "2026-10-01",
            "--end-time",
            "2026-10-02",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 2


def test_requires_station_or_bbox(tmp_path, fetch):
    with patch.dict(os.environ, CREDS), pytest.raises(SystemExit) as exc:
        run_skill(
            fetch,
            "--start-time",
            "2026-10-01",
            "--end-time",
            "2026-10-02",
            "-o",
            str(tmp_path / "o.zarr"),
        )
    assert exc.value.code == 2


def test_unknown_station_exits_2(tmp_path, mod, fetch):
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod, "_get_json", side_effect=_fake_get_json({})),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(
            fetch,
            "--station",
            "99",
            "--start-time",
            "2026-10-01",
            "--end-time",
            "2026-10-02",
            "-o",
            str(tmp_path / "o.zarr"),
        )
    assert exc.value.code == 2


def test_list_stations_tsv_filters_bbox(capsys, mod, fetch):
    with patch.object(mod, "_get_json", side_effect=_fake_get_json({})):
        run_skill(fetch, "--list-stations", "--bbox", "0/35/-2/37")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "station_id\tname\tsite\tlatitude\tlongitude"
    assert lines[1] == "1\tNairobi\tKMD HQ\t-1.30000\t36.76000"
    assert len(lines) == 2


def test_daily_values_and_qc(tmp_path, mod, fetch):
    def make(t):
        return {
            # 0.1 mm every 15 min on day 1 only -> 9.6 mm; gauge 2 agrees.
            "rg1": 0.1 if t.day == 1 else 0.0,
            "rg2": 0.1 if t.day == 1 else 0.0,
            # A -999.9 failed read on day 2 is a missing reading, not a low.
            "mt1": -999.9 if t.day == 2 and t.hour == 3 and t.minute == 0 else 20.0 + t.hour / 2,
            "sh1": 80.0,
            "bp1": 0,  # no pressure sensor fitted
            "ws": 2.0,
            "wg": 5.0 if t.hour == 12 and t.minute == 0 else 3.0,
        }

    ds = _run(tmp_path, mod, fetch, {"1": _readings("2026-10-01", 2, 15, make)})
    assert list(ds.station_id.values) == ["1"]
    precip = ds["precip"].sel(station_id="1").values
    np.testing.assert_allclose(precip, [9.6, 0.0])
    assert ds["tmin"].sel(station_id="1").values[0] == pytest.approx(20.0)
    assert ds["tmax"].sel(station_id="1").values[0] == pytest.approx(31.5)
    # One failed read is a 30-minute gap, inside the 3-interval tolerance.
    assert ds["tmin"].sel(station_id="1").values[1] == pytest.approx(20.0)
    np.testing.assert_allclose(ds["tmin_coverage"].values[:, 0], [1.0, 1.0])
    assert ds["tmin"].attrs["ancillary_variables"] == "tmin_coverage"
    assert ds["tmin_coverage"].attrs["units"] == "1"
    assert ds["wind_gust"].sel(station_id="1").values[0] == pytest.approx(5.0)
    assert "pressure" not in ds  # all readings were the 0 sentinel
    assert "pressure_coverage" not in ds
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert "cell_methods" not in ds["precip"].attrs
    assert ds["humidity"].attrs["units"] == "1"
    assert ds["humidity"].values[0, 0] == pytest.approx(0.8)
    assert ds["latitude"].values[0] == pytest.approx(-1.3)
    assert str(ds["site"].values[0]) == "KMD HQ"
    assert ds.attrs["featureType"] == "timeSeries"


def test_outage_lowers_coverage(tmp_path, mod, fetch):
    def make(t):
        return {"rg1": 0.0, "rg2": 0.0, "mt1": 20.0}

    points = _readings("2026-10-01", 2, 15, make)
    # Drop three hours of day 2.
    points = [
        p
        for p in points
        if not p["time"].startswith(("2026-10-02T10", "2026-10-02T11", "2026-10-02T12"))
    ]
    ds = _run(tmp_path, mod, fetch, {"1": points})
    # 10:00 to 13:00 has no readings: 3 of 24 hours.
    np.testing.assert_allclose(ds["temperature_coverage"].values[:, 0], [1.0, 0.875])
    np.testing.assert_allclose(ds["temperature"].values[:, 0], [20.0, 20.0])
    np.testing.assert_allclose(ds["precip"].values[:, 0], [0.0, 0.0])
    np.testing.assert_allclose(ds["precip_coverage"].values[:, 0], [1.0, 0.875])


def test_partial_day_keeps_the_rain_measured(tmp_path, mod, fetch):
    def make(t):
        return {"rg1": 0.1, "rg2": 0.1, "mt1": 20.0}

    points = _readings("2026-10-01", 2, 15, make)
    # Day 2 loses 00:00-06:00; gauge 2 also misses 12:00, so gauge 1's reading
    # there does not count either.
    points = [p for p in points if not ("2026-10-02T00" <= p["time"] < "2026-10-02T06")]
    for p in points:
        if p["time"] == "2026-10-02T12:00:00Z":
            p["measurements"] = {"rg1": 0.1, "mt1": 20.0}
    # Day 2 covers 75 %, under the default --min-coverage 0.8: NaN, coverage still told.
    ds = _run(tmp_path, mod, fetch, {"1": points})
    np.testing.assert_array_equal(np.isnan(ds["precip"].values[:, 0]), [False, True])
    np.testing.assert_allclose(ds["precip_coverage"].values[:, 0], [1.0, 0.75])
    assert np.isnan(ds["temperature"].values[1, 0])
    # A looser threshold keeps it: 96 readings on day 1; 72 - 1 on day 2, nothing scaled up.
    ds = _run(tmp_path, mod, fetch, {"1": points}, "--min-coverage", "0.5")
    np.testing.assert_allclose(ds["precip"].values[:, 0], [9.6, 7.1])
    np.testing.assert_allclose(ds["temperature"].values[:, 0], [20.0, 20.0])


def test_min_coverage_must_be_a_share(tmp_path, mod, fetch):
    with pytest.raises(SystemExit) as exc:
        _run(
            tmp_path,
            mod,
            fetch,
            {"1": _readings("2026-10-01", 2, 15, _dry)},
            "--min-coverage",
            "80",
        )
    assert exc.value.code == 2


def test_single_missing_reading_keeps_the_day(tmp_path, mod, fetch):
    points = _readings("2026-10-01", 2, 15, _dry)
    missing = ("2026-10-02T14:15:00Z", "2026-10-02T00:00:00Z")  # incl. a day's first
    points = [p for p in points if p["time"] not in missing]
    ds = _run(tmp_path, mod, fetch, {"1": points})
    # The next rain reading holds the missed interval's rain: precip stays complete.
    for name in ("temperature", "precip"):
        assert not np.isnan(ds[name].values).any()
        np.testing.assert_allclose(ds[f"{name}_coverage"].values, 1.0)


def test_sparse_day_is_not_a_longer_interval(tmp_path, mod, fetch):
    # A 1-minute station keeps one reading in four on day 2.
    points = _free_running("2026-10-01", 2, _dry, pad_hours=1)
    points = [p for i, p in enumerate(points) if not (p["time"] >= "2026-10-02" and i % 4)]
    # A 15-minute station reporting on the quarter hour does the same.
    points15 = _readings("2026-10-01", 2, 15, _dry)
    points15 = [p for i, p in enumerate(points15) if not (p["time"] >= "2026-10-02" and i % 4)]
    ds = _run(tmp_path, mod, fetch, {"1": points, "2": points15})
    for sid in ("1", "2"):
        coverage = ds["temperature_coverage"].sel(station_id=sid).values
        assert coverage[0] == pytest.approx(1.0)
        assert coverage[1] < 0.3


def test_one_and_fifteen_minute_stations_both_complete(tmp_path, mod, fetch):
    def wet(t):
        return {"rg1": 0.01, "rg2": 0.01, "mt1": 20.0}

    ds = _run(
        tmp_path,
        mod,
        fetch,
        {
            "1": _free_running("2026-10-01", 2, wet, pad_hours=1),
            "2": _readings("2026-10-01", 2, 15, wet),
        },
    )
    assert not np.isnan(ds["precip"].values).any()
    assert not np.isnan(ds["temperature"].values).any()
    # 96 readings a day at the 15-minute station.
    np.testing.assert_allclose(ds["precip"].sel(station_id="2").values, [0.96, 0.96])


def test_interval_change_mid_request_blanks_the_other_interval(tmp_path, mod, fetch):
    # 1 minute for two days, then 15 minutes: every day is judged at the
    # station's most common spacing (1 minute), so the 15-minute day is NaN.
    points = _free_running("2026-10-01", 2, _dry) + _readings("2026-10-03", 1, 15, _dry)
    ds = _run(tmp_path, mod, fetch, {"1": points}, "--end-time", "2026-10-03")
    coverage = ds["temperature_coverage"].values[:, 0]
    np.testing.assert_allclose(coverage[:2], [1.0, 1.0])
    assert coverage[2] < 0.1


def test_duplicate_readings_count_once(tmp_path, mod, fetch):
    def make(t):
        wet = t == pd.Timestamp("2026-10-01T06:00")
        return {"rg1": 1.0 if wet else 0.0, "rg2": 1.0 if wet else 0.0, "mt1": 20.0}

    points = _readings("2026-10-01", 2, 15, make)
    wet = next(p for p in points if p["time"] == "2026-10-01T06:00:00Z")
    # Day 2 loses 08:00-09:00; a resent copy of another reading does not make up for it.
    points = [p for p in points if not p["time"].startswith("2026-10-02T08")]
    points += [wet, dict(points[-1])]
    ds = _run(tmp_path, mod, fetch, {"1": points})
    np.testing.assert_allclose(ds["precip"].values[0, 0], 1.0)
    assert ds["temperature_coverage"].values[1, 0] == pytest.approx(23 / 24)


def test_rain_gap_up_to_three_intervals_keeps_precip(tmp_path, mod, fetch):
    # A 1-minute station misses two readings in a row on day 2: a 3-minute gap.
    points = _free_running("2026-10-01", 2, _dry, pad_hours=1)
    day2 = [i for i, p in enumerate(points) if p["time"] >= "2026-10-02T12"]
    points = [p for i, p in enumerate(points) if i not in day2[1:3]]
    gap = pd.Timestamp(points[day2[0] + 1]["time"]) - pd.Timestamp(points[day2[0]]["time"])
    assert pd.Timedelta(minutes=3) <= gap < pd.Timedelta(minutes=3.5)  # 3 min plus drift
    ds = _run(tmp_path, mod, fetch, {"1": points})
    for name in ("temperature", "precip"):
        assert not np.isnan(ds[name].values).any()
        np.testing.assert_allclose(ds[f"{name}_coverage"].values, 1.0)


def test_rain_gap_over_three_intervals_lowers_precip_coverage(tmp_path, mod, fetch):
    # Three readings in a row missing: a 4-minute gap loses rain.
    points = _free_running("2026-10-01", 2, _dry, pad_hours=1)
    day2 = [i for i, p in enumerate(points) if p["time"] >= "2026-10-02T12"]
    points = [p for i, p in enumerate(points) if i not in day2[1:4]]
    ds = _run(tmp_path, mod, fetch, {"1": points})
    assert not np.isnan(ds["precip"].values).any()
    assert ds["precip_coverage"].values[0, 0] == 1.0
    assert 0.997 < ds["precip_coverage"].values[1, 0] < 1.0
    assert not np.isnan(ds["temperature"].values).any()


def test_gauges_disagree_gives_nan(tmp_path, mod, fetch):
    def make(t):
        # Gauge 1 is stuck adding 5 mm every reading on day 2; gauge 2 reads dry.
        return {"rg1": 5.0 if t.day == 2 else 0.0, "rg2": 0.0, "mt1": 20.0}

    ds = _run(tmp_path, mod, fetch, {"1": _readings("2026-10-01", 2, 15, make)})
    np.testing.assert_array_equal(np.isnan(ds["precip"].values[:, 0]), [False, True])
    np.testing.assert_allclose(ds["precip_coverage"].values[:, 0], [1.0, 0.0])


def test_day_start_hour_shifts_the_rain_day(tmp_path, mod, fetch):
    def make(t):
        # 1 mm at 03 UTC on 2 Oct: day 2 Oct in UTC, day 1 Oct on a 06 UTC day.
        wet = t == pd.Timestamp("2026-10-02T03:00")
        return {"rg1": 1.0 if wet else 0.0, "rg2": 1.0 if wet else 0.0, "mt1": 20.0}

    points = _readings("2026-10-01T06:00", 2, 15, make)
    ds = _run(tmp_path, mod, fetch, {"1": points}, "--day-start-hour", "6")
    np.testing.assert_allclose(ds["precip"].values[:, 0], [1.0, 0.0])
    assert "06:00" in ds["time"].attrs["comment"]


def test_test_readings_are_dropped(tmp_path, mod, fetch):
    def make(t):
        return {"rg1": 0.0, "rg2": 0.0, "mt1": 20.0}

    points = _readings("2026-10-01", 2, 15, make)
    points.append({"time": "2026-10-01T12:07:00Z", "test": "true", "measurements": {"mt1": 45.0}})
    ds = _run(tmp_path, mod, fetch, {"1": points})
    assert ds["tmax"].values[0, 0] == pytest.approx(20.0)


def test_probe_latest_prints_last_ended_day(capsys, mod, fetch):
    def make(t):
        return {"rg1": 0.0, "rg2": 0.0, "mt1": 20.0}

    today = pd.Timestamp.now(tz="UTC").tz_convert(None).normalize()
    # Readings until 23:45 three days ago: that day has not ended at the newest
    # reading, so the latest ended day is four days ago.
    points = _readings(today - pd.Timedelta(days=10), 8, 15, make)
    # A station whose clock runs ahead is ignored.
    ahead = [{"time": "2099-01-01T00:00:00Z", "test": "false", "measurements": {"mt1": 20.0}}]
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod, "_get_json", side_effect=_fake_get_json({"1": points, "2": ahead})),
    ):
        run_skill(fetch, "--probe-latest", "--station", "1", "--station", "2")
    out = capsys.readouterr().out.strip()
    assert out == (today - pd.Timedelta(days=4)).strftime("%Y-%m-%d")


def test_broken_gauge_is_left_out_for_that_day(tmp_path, mod, fetch, capsys):
    def make(t):
        # Gauge 1 reads 40 mm a minute on day 2 (Voi, Embu in Oct 2026); gauge 2 is fine.
        return {"rg1": 40.0 if t.day == 2 else 0.1, "rg2": 0.1, "mt1": 20.0}

    ds = _run(tmp_path, mod, fetch, {"1": _free_running("2026-10-01", 2, make, pad_hours=1)})
    # Day 1 both gauges agree; day 2 is gauge 2 alone.
    np.testing.assert_allclose(ds["precip"].values[:, 0], [143.1, 143.1], rtol=0.01)
    np.testing.assert_allclose(ds["precip_coverage"].values[:, 0], [1.0, 1.0])
    assert "rg1 over 5 mm/min on 1 day(s)" in capsys.readouterr().err


def test_heavy_rain_at_a_15_minute_station_is_not_broken(tmp_path, mod, fetch):
    def make(t):
        # 30 mm in a quarter hour is 2 mm a minute: heavy, but real.
        wet = t == pd.Timestamp("2026-10-01T06:00")
        return {"rg1": 30.0 if wet else 0.0, "rg2": 30.0 if wet else 0.0, "mt1": 20.0}

    ds = _run(tmp_path, mod, fetch, {"1": _readings("2026-10-01", 2, 15, make)})
    np.testing.assert_allclose(ds["precip"].values[:, 0], [30.0, 0.0])


def test_station_with_only_gauge_2(tmp_path, mod, fetch):
    def make(t):
        return {"rg2": 0.1 if t.day == 1 else 0.0, "mt1": 20.0}

    ds = _run(tmp_path, mod, fetch, {"1": _readings("2026-10-01", 2, 15, make)})
    np.testing.assert_allclose(ds["precip"].values[:, 0], [9.6, 0.0])
    np.testing.assert_allclose(ds["precip_coverage"].values[:, 0], [1.0, 1.0])


def test_gauges_within_2_mm_or_half_agree_and_give_the_larger(tmp_path, mod, fetch):
    def make(t):
        # Day 1: 0 against 1.8 mm (within 2 mm). Day 2: 9.6 against 4.8 (within half).
        first = t == pd.Timestamp("2026-10-01T06:00")
        return {
            "rg1": 0.1 if t.day == 2 else 0.0,
            "rg2": 1.8 if first else (0.05 if t.day == 2 else 0.0),
            "mt1": 20.0,
        }

    ds = _run(tmp_path, mod, fetch, {"1": _readings("2026-10-01", 2, 15, make)})
    np.testing.assert_allclose(ds["precip"].values[:, 0], [1.8, 9.6])
