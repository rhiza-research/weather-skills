"""Unit tests for meteosat-fetch's non-network logic.

Download, satpy calibration, and resampling were verified by running the skill
against the live EUMETSAT Data Store (FCI 0deg, SEVIRI 0deg, SEVIRI IODC over
Kenya/West Africa); these tests cover band/time/instrument resolution, the
geostationary geometry used to pick FCI chunks, and credential lookup.
"""

from datetime import date, datetime, timedelta

import pytest
from conftest import load_skill
from weather_skills_core import DataError, UsageError

KENYA = (5.506, 33.894, -4.677, 41.855)


@pytest.fixture(scope="module")
def mod():
    return load_skill("meteosat-fetch", "fetch")


def test_resolve_bands_shortcuts_and_names(mod):
    assert mod._resolve_bands(["vis"], "seviri") == ["VIS006", "VIS008", "IR_016"]
    assert mod._resolve_bands(["IR"], "fci") == list(mod._BANDS["fci"]["ir"])
    assert mod._resolve_bands(["vis_06,IR_105", "vis_06"], "fci") == ["vis_06", "ir_105"]
    assert mod._resolve_bands(["ir_108"], "seviri") == ["IR_108"]


def test_resolve_bands_rejects_other_instrument_names(mod):
    with pytest.raises(UsageError, match="unknown fci band 'IR_108'"):
        mod._resolve_bands(["IR_108"], "fci")


def test_resolve_instrument_defaults(mod):
    assert mod._resolve_instrument(None, "0deg", date(2026, 10, 6)) == "fci"
    assert mod._resolve_instrument(None, "0deg", date(2024, 9, 23)) == "seviri"
    assert mod._resolve_instrument(None, "iodc", date(2026, 10, 6)) == "seviri"


def test_resolve_instrument_rejects_fci_iodc_and_pre_fci(mod):
    with pytest.raises(UsageError, match="IODC is SEVIRI-only"):
        mod._resolve_instrument("fci", "iodc", date(2026, 10, 6))
    with pytest.raises(UsageError, match="FCI L1c starts"):
        mod._resolve_instrument("fci", "0deg", date(2023, 1, 1))


def test_parse_times_and_slots(mod):
    times = mod._parse_times(date(2026, 10, 6), ["12:10,09:00", "12:10"])
    assert times == [datetime(2026, 10, 6, 9), datetime(2026, 10, 6, 12, 10)]
    assert mod._slot(datetime(2026, 10, 6, 12, 14), "seviri") == datetime(2026, 10, 6, 12, 0)
    assert mod._slot(datetime(2026, 10, 6, 12, 14), "fci") == datetime(2026, 10, 6, 12, 10)
    with pytest.raises(UsageError):
        mod._parse_times(date(2026, 10, 6), ["noon"])


def test_geos_y_matches_proj(mod):
    # Reference values from pyproj `+proj=geos +h=35786400 +ellps=WGS84 +sweep=y`.
    assert mod._geos_y(5.5, 33.9, 0.0) == pytest.approx(586254, abs=1)
    assert mod._geos_y(-35.0, 20.0, 0.0) == pytest.approx(-3479939, abs=1)
    assert mod._geos_y(-4.7, 41.9, 45.5) == pytest.approx(-518569, abs=1)
    assert mod._geos_y(0.0, 100.0, 0.0) is None  # behind the disk


def test_fci_chunks(mod):
    assert mod._fci_chunks(KENYA, 0.0) == list(range(18, 25))
    assert mod._fci_chunks((10, 120, 0, 130), 0.0) == []


def test_entries_to_download(mod):
    body = "W_XX-...-FD--CHK-BODY---NC4E_C_EUMT_x_N__O_0072_{:04d}.nc"
    entries = [body.format(i) for i in range(1, 41)] + [
        "W_XX-...-FD--CHK-TRAIL---NC4E_C_EUMT_x_N__O_0072_0041.nc",
        "manifest.xml",
    ]
    got = mod._entries_to_download(entries, "fci", [19, 20])
    assert got == [body.format(19), body.format(20)]
    assert mod._entries_to_download(["a.nat", "EOPMetadata.xml"], "seviri", []) == ["a.nat"]


def test_grid_snaps_outward(mod):
    w, s, e, n = mod._grid(KENYA, 0.03)
    assert (w, s) == pytest.approx((33.87, -4.68))
    assert (e, n) == pytest.approx((41.88, 5.52))


def test_credentials_env_then_file(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("EUMETSAT_CONSUMER_KEY", "k")
    monkeypatch.setenv("EUMETSAT_CONSUMER_SECRET", "s")
    assert mod._credentials() == ("k", "s")

    monkeypatch.delenv("EUMETSAT_CONSUMER_KEY")
    monkeypatch.delenv("EUMETSAT_CONSUMER_SECRET")
    with pytest.raises(UsageError, match="no EUMETSAT credentials"):
        mod._credentials()

    (tmp_path / ".eumdac").mkdir()
    (tmp_path / ".eumdac" / "credentials").write_text("fk,fs\n")
    assert mod._credentials() == ("fk", "fs")


class _P:
    def __init__(self, start):
        self.sensing_start = start


class _Coll:
    def __init__(self, starts):
        self.starts = starts

    def search(self, dtstart, dtend):
        return [_P(s) for s in self.starts if dtstart <= s < dtend + timedelta(minutes=5)]


def test_find_products_picks_scan_in_slot(mod):
    slot = datetime(2026, 10, 6, 12, 0)
    coll = _Coll([slot + timedelta(seconds=10), slot + timedelta(minutes=15, seconds=10)])
    got = mod._find_products(coll, [slot], "seviri")
    assert got[slot].sensing_start == slot + timedelta(seconds=10)

    with pytest.raises(DataError, match="no seviri scan"):
        mod._find_products(_Coll([]), [slot], "seviri")
