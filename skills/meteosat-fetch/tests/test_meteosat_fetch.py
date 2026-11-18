"""Unit tests for meteosat-fetch's non-network logic.

Download, satpy calibration, and resampling were verified by running the skill
against the live EUMETSAT Data Store (FCI 0deg, SEVIRI 0deg, SEVIRI IODC over
Kenya/West Africa); these tests run fetch() with the Data Store, downloads and
satpy stubbed, covering band/time/instrument resolution, the FCI chunks picked
from the geostationary geometry, credential lookup, and the output grid.
"""

import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill

KENYA_BBOX = "5.506/33.894/-4.677/41.855"
BODY = "W_XX-...-FD--CHK-BODY---NC4E_C_EUMT_x_N__O_0072_{:04d}.nc"


@pytest.fixture(scope="module")
def mod():
    return load_skill("meteosat-fetch", "fetch")


class _Product:
    def __init__(self, start, instrument="fci"):
        self.sensing_start = start
        self.size = 4_000_000
        self.satellite = "MTG-I1" if instrument == "fci" else "MSG4"
        if instrument == "fci":
            self.entries = [BODY.format(i) for i in range(1, 41)]
            self.entries += ["W_XX-...-FD--CHK-TRAIL---NC4E_C_EUMT_x_N__O_0072_0041.nc"]
            self.entries += ["manifest.xml"]
        else:
            self.entries = ["MSG4-SEVI-MSG15-0100-NA-x.nat", "EOPMetadata.xml"]

    def __str__(self):
        return f"scan-{self.sensing_start:%H%M}"


@pytest.fixture
def fake(mod, monkeypatch):
    """Stub the Data Store, downloads and satpy; record what fetch() asked for."""
    calls = {"collection": [], "downloaded": [], "processed": [], "starts": []}

    class Collection:
        def search(self, dtstart, dtend):
            return [
                _Product(s, calls["instrument"]) for s in calls["starts"] if dtstart <= s < dtend
            ]

    def get_collection(cid):
        calls["collection"].append(cid)
        return Collection()

    eumdac = types.ModuleType("eumdac")
    eumdac.AccessToken = lambda credentials: credentials
    eumdac.DataStore = lambda token: types.SimpleNamespace(get_collection=get_collection)
    monkeypatch.setitem(sys.modules, "eumdac", eumdac)
    monkeypatch.setenv("EUMETSAT_CONSUMER_KEY", "k")
    monkeypatch.setenv("EUMETSAT_CONSUMER_SECRET", "s")

    def fake_download(product, entries, workdir, workers):
        calls["downloaded"].append(entries)
        return [workdir / Path(e).name for e in entries]

    def fake_process(files, instrument, bands, bbox):
        calls["processed"].append((instrument, bands))
        n = len(calls["processed"])
        values = {b: np.full((3, 2), 270.0 + 10 * n, dtype="float32") for b in bands}
        attrs = {b: {"units": None, "standard_name": None, "wavelength": None} for b in bands}
        return np.array([0.75, 0.25, -0.25]), np.array([36.25, 36.75]), values, attrs

    monkeypatch.setattr(mod, "_download", fake_download)
    monkeypatch.setattr(mod, "_process_scan", fake_process)

    def run(tmp_path, *args, instrument="fci", starts=None):
        calls["instrument"] = instrument
        calls["starts"] = starts or []
        run_skill(mod.fetch, *args, "-o", str(tmp_path / "out.zarr"))
        return calls

    return run


def _usage_error(capsys):
    return capsys.readouterr().err


def test_bands_shortcuts_and_names(tmp_path, fake):
    starts = [datetime(2026, 10, 6, 12, 0, 5)]
    args = ["--date", "2026-10-06", "--time", "12:00", "--bbox", KENYA_BBOX]
    calls = fake(tmp_path, *args, "-v", "vis_06,IR_105", "-v", "vis_06", starts=starts)
    assert calls["processed"] == [("fci", ["vis_06", "ir_105"])]
    calls = fake(tmp_path, *args, "-v", "IR", starts=starts)
    assert calls["processed"][-1][1] == ["ir_38", "wv_63", "wv_73", "ir_87", "ir_97", "ir_105", "ir_123", "ir_133"]  # fmt: skip


def test_unknown_band_names_the_instrument(tmp_path, fake, capsys):
    with pytest.raises(SystemExit) as exc:
        fake(tmp_path, "--date", "2026-10-06", "--time", "12:00", "--bbox", KENYA_BBOX, "-v", "IR_108")  # fmt: skip
    assert exc.value.code == 2
    assert "unknown fci band 'IR_108'" in _usage_error(capsys)


def test_instrument_defaults_by_service_and_date(tmp_path, fake):
    seviri = [datetime(2024, 9, 23, 12, 0, 5)]
    calls = fake(tmp_path, "--date", "2024-09-23", "--time", "12:00", "--bbox", KENYA_BBOX, "-v", "ir_108", instrument="seviri", starts=seviri)  # fmt: skip
    assert calls["collection"] == ["EO:EUM:DAT:MSG:HRSEVIRI"]
    assert calls["downloaded"] == [["MSG4-SEVI-MSG15-0100-NA-x.nat"]]
    iodc = [datetime(2026, 10, 6, 12, 0, 5)]
    calls = fake(tmp_path, "--date", "2026-10-06", "--time", "12:00", "--bbox", KENYA_BBOX, "-v", "ir_108", "--service", "iodc", instrument="seviri", starts=iodc)  # fmt: skip
    assert calls["collection"][-1] == "EO:EUM:DAT:MSG:HRSEVIRI-IODC"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--instrument", "fci", "--service", "iodc"], "IODC is SEVIRI-only"),
        (["--instrument", "fci", "--date", "2023-01-01"], "FCI L1c starts"),
        (["--time", "noon"], "--time 'noon' is not HH:MM"),
        (["--bbox", "10/120/0/130"], "not visible from the 0deg satellite"),
    ],
)
def test_bad_requests_exit_2(tmp_path, fake, capsys, extra, message):
    args = {"--date": "2026-10-06", "--time": "12:00", "--bbox": KENYA_BBOX, "-v": "ir_105"}
    for flag, value in zip(extra[::2], extra[1::2], strict=True):
        args[flag] = value
    with pytest.raises(SystemExit) as exc:
        fake(tmp_path, *[x for kv in args.items() for x in kv])
    assert exc.value.code == 2
    assert message in _usage_error(capsys)


def test_geos_y_matches_proj(mod):
    # Reference values from pyproj `+proj=geos +h=35786400 +ellps=WGS84 +sweep=y`.
    assert mod._geos_y(5.5, 33.9, 0.0) == pytest.approx(586254, abs=1)
    assert mod._geos_y(-35.0, 20.0, 0.0) == pytest.approx(-3479939, abs=1)
    assert mod._geos_y(-4.7, 41.9, 45.5) == pytest.approx(-518569, abs=1)
    assert mod._geos_y(0.0, 100.0, 0.0) is None  # behind the disk


def test_kenya_downloads_fci_chunks_18_to_24(tmp_path, fake):
    starts = [datetime(2026, 10, 6, 12, 0, 5)]
    calls = fake(tmp_path, "--date", "2026-10-06", "--time", "12:00", "--bbox", KENYA_BBOX, "-v", "ir_105", starts=starts)  # fmt: skip
    assert calls["downloaded"] == [[BODY.format(i) for i in range(18, 25)]]


def test_scan_must_start_in_its_slot(tmp_path, fake, capsys):
    # A scan starting in the next cycle does not stand in for 12:00.
    with pytest.raises(SystemExit) as exc:
        fake(tmp_path, "--date", "2026-10-06", "--time", "12:00", "--bbox", KENYA_BBOX, "-v", "ir_105", starts=[datetime(2026, 10, 6, 12, 10, 5)])  # fmt: skip
    assert exc.value.code != 0
    assert "no fci scan for 2026-10-06 12:00 UTC" in capsys.readouterr().err


def test_credentials_from_file(tmp_path, fake, monkeypatch):
    monkeypatch.delenv("EUMETSAT_CONSUMER_KEY")
    monkeypatch.delenv("EUMETSAT_CONSUMER_SECRET")
    monkeypatch.setenv("HOME", str(tmp_path))
    args = ["--date", "2026-10-06", "--time", "12:00", "--bbox", KENYA_BBOX, "-v", "ir_105"]
    with pytest.raises(SystemExit) as exc:
        fake(tmp_path, *args)
    assert exc.value.code == 2

    (tmp_path / ".eumdac").mkdir()
    (tmp_path / ".eumdac" / "credentials").write_text("fk,fs\n")
    calls = fake(tmp_path, *args, starts=[datetime(2026, 10, 6, 12, 0, 5)])
    assert calls["processed"]


def test_fetch_end_to_end(tmp_path, fake):
    slots = [datetime(2026, 10, 6, 12, 0), datetime(2026, 10, 6, 12, 10)]
    starts = [s + timedelta(seconds=5) for s in slots]
    # 12:14 floors to the 12:10 FCI cycle.
    fake(tmp_path, "--date", "2026-10-06", "--time", "12:00,12:14", "--bbox", "1/36/-0.5/37", "-v", "vis_06,ir_105", starts=starts)  # fmt: skip

    ds = xr.open_zarr(tmp_path / "out.zarr")
    assert ds["ir_105"].dims == ("time", "latitude", "longitude")
    assert dict(ds.sizes) == {"time": 2, "latitude": 3, "longitude": 2}
    np.testing.assert_array_equal(ds["time"].values, pd.DatetimeIndex(slots).values)
    np.testing.assert_array_equal(ds["scan_start"].values, pd.DatetimeIndex(starts).values)
    np.testing.assert_allclose(ds["ir_105"].values[:, 0, 0], [280.0, 290.0])
    # satpy attrs missing -> fallback units and standard_name, no wavelength.
    assert ds["vis_06"].attrs["units"] == "%"
    assert ds["vis_06"].attrs["standard_name"] == "toa_bidirectional_reflectance"
    assert ds["ir_105"].attrs["units"] == "K"
    assert ds["ir_105"].attrs["standard_name"] == "toa_brightness_temperature"
    assert "central_wavelength_um" not in ds["ir_105"].attrs
    assert ds.attrs["weather_skills_source"] == "eumetsat-datastore:EO:EUM:DAT:0662"
    assert ds.attrs["platform"] == "MTG-I1"
    assert ds.attrs["instrument"] == "FCI"
    assert ds.attrs["eumetsat_service"] == "0deg"
    assert ds.attrs["sub_satellite_longitude"] == 0.0


def test_process_scan_snaps_grid_outward(mod, monkeypatch):
    """The bbox snaps outward to the native grid; 1-D centres come from its extent."""
    made = {}

    class Band:
        def __init__(self):
            self.attrs = {"area": types.SimpleNamespace(pixel_size_x=3000.4), "units": "K"}

    class Scene:
        def __init__(self, reader, filenames):
            pass

        def load(self, bands):
            self.bands = bands

        def crop(self, ll_bbox):
            return self

        def __getitem__(self, band):
            return Band()

        def resample(self, area, resampler, radius_of_influence):
            made["area"] = area
            data = np.zeros((area.height, area.width))
            return {b: types.SimpleNamespace(data=data) for b in self.bands}

    def create_area_def(name, crs, area_extent, resolution, units):
        w, s, e, n = area_extent
        made["extent"] = area_extent
        return types.SimpleNamespace(
            width=round((e - w) / resolution),
            height=round((n - s) / resolution),
            pixel_size_x=resolution,
            pixel_size_y=resolution,
        )

    monkeypatch.setitem(sys.modules, "satpy", types.SimpleNamespace(Scene=Scene))
    monkeypatch.setitem(sys.modules, "pyresample", types.SimpleNamespace(create_area_def=create_area_def))  # fmt: skip
    monkeypatch.setitem(sys.modules, "dask", types.SimpleNamespace(compute=lambda *a: a))

    bbox = (5.506, 33.894, -4.677, 41.855)
    lat, lon, values, attrs = mod._process_scan([], "seviri", ["IR_108"], bbox)
    # SEVIRI 3 km -> 0.03 degrees.
    assert made["extent"] == pytest.approx((33.87, -4.68, 41.88, 5.52))
    np.testing.assert_allclose(lon[:2], [33.885, 33.915])
    np.testing.assert_allclose(lat[:2], [5.505, 5.475])
    assert values["IR_108"].shape == (len(lat), len(lon))
    assert attrs["IR_108"]["units"] == "K"


def test_probe_latest_prints_the_newest_scan_day(tmp_path, fake, capsys):
    newest = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)).replace(
        minute=14, second=5, microsecond=0
    )
    fake(tmp_path, "--date", "2026-10-06", "--time", "12:00", "--bbox", KENYA_BBOX, "-v", "ir_105", "--probe-latest", starts=[newest - timedelta(minutes=10), newest])  # fmt: skip
    out = capsys.readouterr()
    assert out.out.strip() == f"{newest:%Y-%m-%d}"
    # Floored to the 10-minute FCI cycle.
    assert f"latest scan {newest:%Y-%m-%d %H}:10 UTC" in out.err
