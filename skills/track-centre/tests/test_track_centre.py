"""Correctness tests for track-centre."""

import json

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def track_centre():
    return load_skill("track-centre", "track_centre").track_centre


def _ensemble(n_member=8, fill_member=None, sign=-1.0):
    """6-hourly MSLP: a 10 hPa low moving 1 deg west per lead from (-6, 50).

    Member m sits 0.25 * m deg further south from the second lead on, so the
    spread is known. ``fill_member`` loses its low after the third lead.
    """
    lats, lons = np.arange(-16.0, 4.01, 0.25), np.arange(36.0, 60.01, 0.25)
    lat, lon = np.meshgrid(lats, lons, indexing="ij")
    steps = np.arange(1, 7) * np.timedelta64(6, "h")
    data = np.empty((n_member, steps.size, lats.size, lons.size), dtype="float32")
    truth = np.empty((n_member, steps.size, 2))
    for m in range(n_member):
        for t in range(steps.size):
            c = (-6.0 - (0.25 * m if t else 0.0), 50.0 - t)
            amp = 0.0 if m == fill_member and t >= 3 else 10.0
            data[m, t] = 1012 + sign * amp * np.exp(-((lat - c[0]) ** 2 + (lon - c[1]) ** 2) / 2)
            truth[m, t] = c
    ds = xr.Dataset(
        {"msl": (("number", "step", "latitude", "longitude"), data)},
        coords={
            "number": np.arange(n_member),
            "step": steps.astype("timedelta64[ns]"),
            "time": np.datetime64("2026-10-23", "ns"),
            "latitude": lats,
            "longitude": lons,
        },
    )
    ds["msl"].attrs.update(units="hPa", standard_name="air_pressure_at_mean_sea_level")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east")
    return ds, truth


def _run(track_centre, tmp_path, ds, *args):
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(track_centre, "-i", str(src), "-o", str(out), *args)
    return out, xr.open_zarr(out).load()


def test_tracks_follow_each_member_and_build_the_funnel(tmp_path, track_centre):
    ds, truth = _ensemble(fill_member=7)
    geojson = tmp_path / "funnel.geojson"
    out, tr = _run(track_centre, tmp_path, ds, "--start=-6/50", "--funnel-geojson", str(geojson))
    assert tr["track_latitude"].dims == ("number", "step")
    got = np.stack([tr["track_latitude"].values, tr["track_longitude"].values], axis=-1)
    # Every position up to the filled member's last lead is the true centre.
    np.testing.assert_allclose(got[:7], truth[:7], atol=0.01)
    np.testing.assert_allclose(got[7, :3], truth[7, :3], atol=0.01)
    assert np.isnan(got[7, 3:]).all()
    assert tr["members"].values.tolist() == [8, 8, 8, 7, 7, 7]
    np.testing.assert_allclose(tr["mean_longitude"].values, 50.0 - np.arange(6), atol=0.01)
    np.testing.assert_allclose(tr["mean_latitude"].values[3], np.mean(truth[:7, 3, 0]), atol=0.01)
    # 90% of members within the radius; the first lead has no spread (one cell).
    assert tr["funnel_radius"].values[0] == pytest.approx(0.25)
    assert tr["funnel_radius"].values[1] > 0.5
    np.testing.assert_allclose(tr["msl"].values[0, 0], 1002, atol=0.05)
    assert tr["depth"].values[0, 0] > 5
    assert tr.attrs["featureType"] == "trajectory" and tr.attrs["funnel_percent"] == 90
    assert load_history(out)[-1]["skill"] == "track-centre"
    shape = json.loads(geojson.read_text())["features"][0]["geometry"]
    assert shape["type"] == "MultiPolygon"
    xs = [x for poly in shape["coordinates"] for ring in poly for x, _ in ring]
    assert min(xs) < 45 and max(xs) > 50


def test_high_is_tracked_like_a_low(tmp_path, track_centre):
    ds, truth = _ensemble(n_member=5, sign=1.0)
    _, tr = _run(track_centre, tmp_path, ds, "--start=-6/50", "--kind", "high")
    np.testing.assert_allclose(tr["track_longitude"].values, truth[..., 1], atol=0.01)
    assert tr.attrs["track_kind"] == "high"


def test_single_member_without_number_dim(tmp_path, track_centre):
    ds, truth = _ensemble(n_member=1)
    _, tr = _run(track_centre, tmp_path, ds.isel(number=0, drop=True), "--start=-6/50")
    assert tr["members"].values.tolist() == [1] * 6
    np.testing.assert_allclose(tr["mean_longitude"].values, truth[0, :, 1], atol=0.01)


def test_start_far_from_any_low_exits_1(tmp_path, track_centre):
    ds, _ = _ensemble()
    with pytest.raises(SystemExit) as exc:
        _run(track_centre, tmp_path, ds, "--start=0/38")
    assert exc.value.code == 1


@pytest.mark.parametrize("args", [["--start", "6,50"], ["--start=-6/50", "--max-move", "0"]])
def test_bad_arguments_exit_2(tmp_path, track_centre, args):
    ds, _ = _ensemble()
    with pytest.raises(SystemExit) as exc:
        _run(track_centre, tmp_path, ds, *args)
    assert exc.value.code == 2


def test_extra_dim_must_be_selected_first(tmp_path, track_centre):
    ds, _ = _ensemble()
    ds = ds.expand_dims(level=[1000])
    with pytest.raises(SystemExit) as exc:
        _run(track_centre, tmp_path, ds, "--start=-6/50")
    assert exc.value.code == 2


def test_every_keeps_only_leads_on_the_interval(tmp_path, track_centre):
    ds, truth = _ensemble(n_member=5)
    # Relabel the 6-hourly leads as 3-hourly: only 6, 12 and 18 h stay tracked.
    ds = ds.assign_coords(step=ds["step"].values // 2)
    _, tr = _run(track_centre, tmp_path, ds, "--start=-6/50", "--every", "6h")
    assert (tr["step"].values / np.timedelta64(1, "h")).tolist() == [6, 12, 18]
    np.testing.assert_allclose(tr["mean_longitude"].values, truth[0, 1::2, 1], atol=0.01)
