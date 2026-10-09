"""Correctness tests for kmsa-wrf-fetch (SSH/SFTP mocked)."""

import shutil
from contextlib import contextmanager

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill
from weather_skills_core.provenance import load_history

FILES = [
    "20261004_to_20261011_fcst.nc",
    "20261004_to_20261011_fcst.txt",
    "20261005_to_20261012_fcst.nc",
    "notes.nc",
]
LATS = [-1.0, -0.964, 0.0]
LONS = [36.0, 36.036, 36.072, 36.108]


@pytest.fixture(scope="module")
def fetch_mod():
    return load_skill("kmsa-wrf-fetch", "fetch")


def _wrf_file(path, init="2026-10-05T06:00", n_time=5, rainc_rate=0.0, rainnc_rate=2.0):
    """A file in the KMSA WRF layout: (time, lev, lat, lon), cumulative rain in mm every 3 h."""
    times = np.datetime64(init, "ns") + np.arange(n_time) * np.timedelta64(3, "h")
    shape = (n_time, 1, len(LATS), len(LONS))
    ramp = np.arange(n_time, dtype="float32")[:, None, None, None]
    ds = xr.Dataset(
        {
            "rainc": (("time", "lev", "lat", "lon"), np.broadcast_to(ramp * rainc_rate, shape)),
            "rainnc": (("time", "lev", "lat", "lon"), np.broadcast_to(ramp * rainnc_rate, shape)),
            "t2": (("time", "lev", "lat", "lon"), np.broadcast_to(290.0 + ramp, shape)),
        },
        coords={"time": times, "lev": [1000.0], "lat": LATS, "lon": LONS},
    )
    ds["rainc"].attrs["long_name"] = "ACCUMULATED TOTAL CUMULUS PRECIPITATION (mm)"
    ds["rainnc"].attrs["long_name"] = "ACCUMULATED TOTAL GRID SCALE PRECIPITATION (mm)"
    ds["t2"].attrs["long_name"] = "TEMP at 2 M (K)"
    ds["lat"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["lon"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds.to_netcdf(path, encoding={"time": {"units": "hours since 1-1-1 00:00:00"}})
    return path


class FakeSFTP:
    def __init__(self, src):
        self.src = src
        self.got = []

    def listdir(self, folder):
        self.folder = folder
        return list(FILES)

    def get(self, remote, local):
        self.got.append(remote)
        shutil.copy(self.src, local)


@pytest.fixture
def remote(fetch_mod, monkeypatch, tmp_path):
    """Mock the SFTP session: FILES in the folder, every download returns one WRF file."""
    sftp = FakeSFTP(_wrf_file(tmp_path / "src.nc"))

    @contextmanager
    def fake_sftp():
        yield sftp

    monkeypatch.setattr(fetch_mod, "_sftp", fake_sftp)
    monkeypatch.setenv("KMSA_WRF_REMOTE_DIR", "/data/wrf/")
    return sftp


def test_fetch_latest_deaccumulates_to_rates(tmp_path, fetch_mod, remote):
    out = tmp_path / "wrf.zarr"
    run_skill(fetch_mod.fetch, "--source", "ssh", "-o", str(out))

    assert remote.got == ["/data/wrf/20261005_to_20261012_fcst.nc"]
    with xr.open_zarr(out, consolidated=True) as ds:
        assert list(ds.data_vars) == ["tp"]
        assert ds["tp"].dims == ("step", "latitude", "longitude")
        assert ds["tp"].attrs["units"] == "mm day-1"
        assert ds["time"].values == np.datetime64("2026-10-05T06:00", "ns")
        hours = ds["step"].values.astype("timedelta64[h]").astype(int)
        assert list(hours) == [0, 3, 6, 9]
        # 2 mm per 3 h -> 16 mm day-1 in every interval.
        np.testing.assert_allclose(ds["tp"].values[:, 0, 0], 16.0, rtol=1e-6)
        assert ds.attrs["weather_skills_source"] == "kmsa-wrf:ssh:20261005_to_20261012_fcst.nc"
    assert load_history(out)[-1]["skill"] == "kmsa-wrf-fetch"


def test_tp_sums_cumulus_and_grid_scale(tmp_path, fetch_mod):
    src = _wrf_file(tmp_path / "20261005_to_20261012_fcst.nc", rainc_rate=1.0, rainnc_rate=2.0)
    out = tmp_path / "wrf_sum.zarr"
    run_skill(fetch_mod.fetch, "--input-file", str(src), "-v", "tp", "-v", "rainc", "-o", str(out))
    with xr.open_zarr(out, consolidated=True) as ds:
        np.testing.assert_allclose(ds["tp"].values[:, 1, 1], 24.0, rtol=1e-6)
        np.testing.assert_allclose(ds["rainc"].values[:, 1, 1], 8.0, rtol=1e-6)


def test_t2m_in_celsius_on_interval_start(tmp_path, fetch_mod):
    src = _wrf_file(tmp_path / "20261005_to_20261012_fcst.nc")
    out = tmp_path / "wrf_t2m.zarr"
    run_skill(fetch_mod.fetch, "--input-file", str(src), "-v", "t2m", "-v", "tp", "-o", str(out))
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds["t2m"].attrs["units"] == "degree_Celsius"
        np.testing.assert_allclose(
            ds["t2m"].values[:, 0, 0], [16.85, 17.85, 18.85, 19.85], atol=1e-4
        )


def test_date_and_bbox(tmp_path, fetch_mod, remote):
    out = tmp_path / "wrf_bbox.zarr"
    run_skill(
        fetch_mod.fetch,
        "--source",
        "ssh",
        "--date",
        "2026-10-04",
        "--bbox",
        "0/36/-1/36.05",
        "--remote-dir",
        "/other",
        "-o",
        str(out),
    )
    assert remote.folder == "/other"
    assert remote.got == ["/other/20261004_to_20261011_fcst.nc"]
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds.sizes["latitude"] == 3
        assert ds.sizes["longitude"] == 2


def test_probe_latest_ssh(capsys, fetch_mod, remote):
    run_skill(fetch_mod.fetch, "--source", "ssh", "--probe-latest")
    assert capsys.readouterr().out.strip() == "2026-10-05"


def test_missing_date_exits_1(tmp_path, fetch_mod, remote):
    with pytest.raises(SystemExit) as exc:
        run_skill(
            fetch_mod.fetch,
            "--source",
            "ssh",
            "--date",
            "2020-01-01",
            "-o",
            str(tmp_path / "unused.zarr"),
        )
    assert exc.value.code == 1


def test_unknown_variable_exits_2(tmp_path, fetch_mod, remote):
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "-v", "msl", "-o", str(tmp_path / "unused.zarr"))
    assert exc.value.code == 2


def test_no_remote_dir_exits_2(tmp_path, fetch_mod, monkeypatch):
    monkeypatch.delenv("KMSA_WRF_REMOTE_DIR", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_skill(fetch_mod.fetch, "--source", "ssh", "-o", str(tmp_path / "unused.zarr"))
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("host", "env", "expected"),
    [
        ("wrf.example", {"KMSA_WRF_SSH_USER": "kmsa"}, ("wrf.example", 22, "kmsa")),
        ("kmsa@wrf.example", {}, ("wrf.example", 22, "kmsa")),
        ("kmsa@wrf.example:2222", {}, ("wrf.example", 2222, "kmsa")),
        (
            "wrf.example",
            {"KMSA_WRF_SSH_USER": "u", "KMSA_WRF_SSH_PORT": "2200"},
            ("wrf.example", 2200, "u"),
        ),
    ],
)
def test_connection_settings_forms(fetch_mod, monkeypatch, host, env, expected):
    for name in ("KMSA_WRF_SSH_USER", "KMSA_WRF_SSH_PORT", "KMSA_WRF_SSH_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KMSA_WRF_SSH_HOST", host)
    monkeypatch.setenv("KMSA_WRF_SSH_PASSWORD", "pw")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    s = fetch_mod._connection_settings()
    assert (s["hostname"], s["port"], s["username"]) == expected


def test_connection_needs_password_or_key(fetch_mod, monkeypatch):
    monkeypatch.setenv("KMSA_WRF_SSH_HOST", "kmsa@wrf.example")
    monkeypatch.delenv("KMSA_WRF_SSH_PASSWORD", raising=False)
    monkeypatch.delenv("KMSA_WRF_SSH_KEY", raising=False)
    with pytest.raises(fetch_mod.UsageError):
        fetch_mod._connection_settings()


@pytest.fixture
def bucket(fetch_mod, monkeypatch, tmp_path):
    """Mock the public bucket listing and object download."""
    src = _wrf_file(tmp_path / "src.nc")
    seen = {"got": []}

    def fake_list(gcs_path):
        seen["listed"] = gcs_path
        return sorted(n for n in FILES if fetch_mod._FILE_RE.fullmatch(n))

    def fake_download(gcs_path, file_name, local):
        seen["got"].append((gcs_path, file_name))
        shutil.copy(src, local)

    monkeypatch.setattr(fetch_mod, "_gcs_list", fake_list)
    monkeypatch.setattr(fetch_mod, "_gcs_download", fake_download)
    return seen


def test_gcs_is_default_source(tmp_path, fetch_mod, bucket):
    out = tmp_path / "wrf_gcs.zarr"
    run_skill(fetch_mod.fetch, "--date", "2026-10-04", "-o", str(out))
    assert bucket["got"] == [
        ("sheerwater-public-datalake/kmsa-wrf", "20261004_to_20261011_fcst.nc")
    ]
    with xr.open_zarr(out, consolidated=True) as ds:
        assert ds.attrs["weather_skills_source"] == (
            "kmsa-wrf:gs://sheerwater-public-datalake/kmsa-wrf/20261004_to_20261011_fcst.nc"
        )
        np.testing.assert_allclose(ds["tp"].values[:, 0, 0], 16.0, rtol=1e-6)


def test_probe_latest_gcs(capsys, fetch_mod, bucket):
    run_skill(fetch_mod.fetch, "--probe-latest", "--gcs-path", "gs://other-bucket/wrf/")
    assert bucket["listed"] == "gs://other-bucket/wrf/"
    assert capsys.readouterr().out.strip() == "2026-10-05"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("sheerwater-public-datalake/kmsa-wrf", ("sheerwater-public-datalake", "kmsa-wrf/")),
        ("gs://b/a/b/", ("b", "a/b/")),
        ("b", ("b", "")),
    ],
)
def test_split_gcs_path(fetch_mod, path, expected):
    assert fetch_mod._split_gcs_path(path) == expected
