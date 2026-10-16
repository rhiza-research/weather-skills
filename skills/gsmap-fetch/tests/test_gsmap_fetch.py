"""Correctness tests for gsmap-fetch (jaxa-earth replaced by a fake module)."""

import sys
import types
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def fetch():
    return load_skill("gsmap-fetch", "fetch").fetch


class _FakeCollection:
    """Mimics the jaxa-earth calls fetch.py makes, including its DN masking.

    jaxa-earth reads ``min``/``max``/``nodata`` from the FIRST STAC item only
    (``stac_bounds.json[0][0]``) and NaNs every pixel of every image outside
    that range -- this fake does the same.
    """

    def __init__(self, days, calls):
        self._days = days
        self._calls = calls

    def filter_date(self, dlim):
        day = dlim[0][:10]
        if day not in self._days:
            raise Exception("Error! No date list found!")
        self._day = day
        ids = [f"{day[:7]}/{day[8:10]}-{h:02d}/" for h in self._days[day]["hours"]]
        self.stac_date = types.SimpleNamespace(id=ids)
        return self

    def filter_resolution(self, ppu):
        return self

    def filter_bounds(self, bbox):
        self._calls.append(bbox)
        dn = {"min": 0.0, "max": 5.0, "nodata": -999.9, "error": [-4.0, -8.0, -99.0]}
        item = {"assets": {"PRECIP": {"je:rasters": {"dn": dn}}}}
        self.stac_bounds = types.SimpleNamespace(json=[[item]])
        return self

    def select(self, band):
        return self

    def get_images(self):
        dn = self.stac_bounds.json[0][0]["assets"]["PRECIP"]["je:rasters"]["dn"]
        img = np.array(self._days[self._day]["img"], dtype="float32")
        out = img.copy()
        if "min" in dn and "max" in dn:
            out[(img < dn["min"]) | (img > dn["max"])] = np.nan
        out[img == np.float32(dn["nodata"])] = np.nan
        raster = types.SimpleNamespace(
            img=out[..., None], latlim=[[0.0, 0.2]], lonlim=[[36.0, 36.2]]
        )
        return types.SimpleNamespace(raster=raster)


def _fake_jaxa(days, calls):
    je = types.SimpleNamespace(ImageCollection=lambda collection: _FakeCollection(days, calls))
    earth = types.ModuleType("jaxa.earth")
    earth.je = je
    jaxa = types.ModuleType("jaxa")
    jaxa.earth = earth
    return {"jaxa": jaxa, "jaxa.earth": earth}


def _run(fetch, days, start, end, bbox, out, calls=None):
    with patch.dict(sys.modules, _fake_jaxa(days, [] if calls is None else calls)):
        run_skill(fetch, "--start-time", start, "--end-time", end, "--bbox", bbox, "-o", str(out))


# Two hours on a 2x2 grid. Hour 03 holds rain far above the first image's own
# max (5 mm/hr), an error code (-4) and nodata (-999.9).
_DAY = {
    "hours": [2, 3],
    "img": [[[1.0, 2.0], [3.0, 4.0]], [[50.0, -4.0], [-999.9, 0.5]]],
}


def test_rain_above_first_images_max_is_kept_and_error_codes_are_nan(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    _run(fetch, {"2026-10-01": _DAY}, "2026-10-01", "2026-10-01", "0.2/36/0/36.2", out)

    precip = xr.open_zarr(out)["precip"]
    assert precip.attrs["units"] == "mm day-1"
    assert precip.attrs["data_interval"] == "1 hour"
    hour3 = precip.sel(time="2026-10-01T03:00").values
    np.testing.assert_allclose(hour3[0, 0], 50.0 * 24)  # was NaN: above the first image's max
    assert np.isnan(hour3[0, 1]) and np.isnan(hour3[1, 0])  # -4 error code, -999.9 nodata
    np.testing.assert_allclose(hour3[1, 1], 0.5 * 24)


def test_hours_labelled_from_ids_on_cell_centres(tmp_path, fetch):
    out = tmp_path / "out.zarr"
    _run(fetch, {"2026-10-01": _DAY}, "2026-10-01", "2026-10-01", "0.2/36/0/36.2", out)

    ds = xr.open_zarr(out)
    assert list(ds.time.values.astype("datetime64[h]").astype(str)) == [
        "2026-10-01T02",
        "2026-10-01T03",
    ]
    np.testing.assert_allclose(ds.latitude.values, [0.15, 0.05])
    np.testing.assert_allclose(ds.longitude.values, [36.05, 36.15])


def test_unpublished_day_is_skipped_not_filled(tmp_path, fetch, capsys):
    out = tmp_path / "out.zarr"
    _run(fetch, {"2026-10-12": _DAY}, "2026-10-12", "2026-10-13", "0.2/36/0/36.2", out)

    ds = xr.open_zarr(out)
    assert ds.sizes["time"] == 2
    err = capsys.readouterr().err
    assert "no images for 2026-10-13" in err
    assert "2 of 48 hours served" in err


def test_no_images_in_range_exits_1(tmp_path, fetch):
    with pytest.raises(SystemExit) as exc:
        _run(fetch, {}, "2025-03-31", "2025-03-31", "0.2/36/0/36.2", tmp_path / "out.zarr")
    assert exc.value.code == 1


def test_bbox_beyond_60_degrees_exits_2(tmp_path, fetch):
    with pytest.raises(SystemExit) as exc:
        _run(
            fetch,
            {"2026-10-01": _DAY},
            "2026-10-01",
            "2026-10-01",
            "70/10/58/20",
            tmp_path / "o.zarr",
        )
    assert exc.value.code == 2


def test_0_360_longitudes_are_wrapped(tmp_path, fetch):
    calls = []
    _run(
        fetch,
        {"2026-10-01": _DAY},
        "2026-10-01",
        "2026-10-01",
        "1/200/0/201",
        tmp_path / "o.zarr",
        calls,
    )
    assert calls == [[-160.0, 0.0, -159.0, 1.0]]


def test_antimeridian_bbox_exits_2(tmp_path, fetch):
    with pytest.raises(SystemExit) as exc:
        _run(
            fetch,
            {"2026-10-01": _DAY},
            "2026-10-01",
            "2026-10-01",
            "5/175/0/-175",
            tmp_path / "o.zarr",
        )
    assert exc.value.code == 2
