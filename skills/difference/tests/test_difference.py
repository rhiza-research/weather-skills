"""Correctness tests for difference."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def difference():
    return load_skill("difference", "difference").difference


def test_difference_subtracts_b_from_a(tmp_path, difference):
    a = write_zarr(make_gridded(fill=5.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "out.zarr"

    run_skill(difference, "-i", str(a), "-i", str(b), "-o", str(out))

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds["precip"].values == pytest.approx(3.0)
    assert load_history(out)[-1]["skill"] == "difference"


def test_difference_requires_two_inputs(tmp_path, difference):
    a = write_zarr(make_gridded(), tmp_path / "a.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(difference, "-i", str(a), "-o", str(out))
    assert exc.value.code == 2


def _field(name, value, units, lats=(1.0, 2.0), standard_name=None):
    attrs = {"units": units}
    if standard_name:
        attrs["standard_name"] = standard_name
    return xr.Dataset(
        {name: (("latitude", "longitude"), np.full((len(lats), 2), value), attrs)},
        coords={"latitude": list(lats), "longitude": [10.0, 11.0]},
    )


def _run(tmp_path, difference, a, b, *args):
    pa = write_zarr(a, tmp_path / "a.zarr")
    pb = write_zarr(b, tmp_path / "b.zarr")
    out = tmp_path / "out.zarr"
    run_skill(difference, "-i", str(pa), "-i", str(pb), "-o", str(out), *args)
    return xr.open_zarr(out, consolidated=True)


def test_difference_converts_units_and_keeps_temperature_differences(tmp_path, difference):
    t = "air_temperature"
    r = _run(
        tmp_path,
        difference,
        _field("t2m", 27.0, "degree_Celsius", standard_name=t),
        _field("t2m", 298.15, "K", standard_name=t),
    )
    assert r["t2m"].values == pytest.approx(2.0)
    assert r["t2m"].attrs["units"] == "delta_degC"

    # K minus K (or minus degC) is still a difference, not an absolute kelvin
    # value that unit-convert would shift by 273.15
    for b in (_field("t2m", 298.15, "K", standard_name=t), _field("t2m", 25.0, "degC")):
        r = _run(tmp_path, difference, _field("t2m", 300.15, "K", standard_name=t), b)
        assert r["t2m"].values == pytest.approx(2.0)
        assert r["t2m"].attrs["units"] == "delta_degC"

    r = _run(
        tmp_path, difference, _field("precip", 24.0, "mm day-1"), _field("precip", 0.5, "mm hr-1")
    )
    assert r["precip"].values == pytest.approx(12.0)


@pytest.mark.parametrize(
    "b, args",
    [
        (_field("precip", 14.0, "mm"), ()),  # rate minus amount
        (_field("precip", 1.0, "mm day-1", lats=(1.05, 2.05)), ()),  # no shared labels
        (_field("tp", 1.0, "mm day-1"), ()),  # no shared variable
        (_field("tp", 1.0, "mm day-1"), ("-v", "precip")),  # named var missing from B
    ],
)
def test_difference_refuses_instead_of_writing_garbage(tmp_path, difference, b, args):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, difference, _field("precip", 2.0, "mm day-1"), b, *args)
    assert exc.value.code == 2
    assert not (tmp_path / "out.zarr").exists()


def test_difference_keeps_only_a_attrs(tmp_path, difference):
    # A daily field minus a weekly baseline must not inherit the baseline's
    # aggregation_period (plot would multiply the daily rate by 7)
    a = _field("precip", 3.0, "mm day-1")
    b = _field("precip", 1.0, "mm day-1")
    b["precip"].attrs.update(aggregation_period="7 day", cell_methods="time: mean")
    r = _run(tmp_path, difference, a, b)
    assert "aggregation_period" not in r["precip"].attrs
    assert "cell_methods" not in r["precip"].attrs


def _timed(dim, labels, value=1.0, units="mm day-1"):
    return xr.Dataset(
        {"precip": ((dim, "latitude"), np.full((len(labels), 2), value), {"units": units})},
        coords={dim: labels, "latitude": [1.0, 2.0]},
    )


def _total(period):
    ds = _field("precip", 10.0, "mm")
    ds["precip"].attrs.update(aggregation_period=period, cell_methods="time: sum")
    return ds


@pytest.mark.parametrize(
    "a, b",
    [
        # time series minus a dayofyear climatology: would broadcast time x dayofyear
        (_timed("time", np.arange(3).astype("datetime64[D]")), _timed("dayofyear", [1, 2])),
        (_total("7 day"), _total("30 day")),  # weekly total minus monthly total
    ],
)
def test_difference_refuses_mismatched_axes_and_periods(tmp_path, difference, a, b):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, difference, a, b)
    assert exc.value.code == 2
    assert not (tmp_path / "out.zarr").exists()


def test_difference_relabels_long_name(tmp_path, difference):
    """An SST anomaly's colorbar must not read as the raw quantity."""
    a = _field("sst", 28.0, "degC")
    a["sst"].attrs.update(
        long_name="Sea Surface Temperature", standard_name="sea_surface_temperature"
    )
    b = _field("sst", 27.0, "degC")
    r = _run(tmp_path, difference, a, b)
    assert r["sst"].attrs["long_name"] == "Sea Surface Temperature difference"
    assert r["sst"].attrs["standard_name"] == "sea_surface_temperature"

    b.attrs["climatology_dataset"] = "oisst"  # a climatology B is still A - B
    r = _run(tmp_path / "clim", difference, a, b)
    assert r["sst"].attrs["long_name"] == "Sea Surface Temperature difference"


def test_difference_no_shared_variable_names_the_rename(tmp_path, difference, capsys):
    a = _field("precip", 3.0, "mm day-1")
    b = _field("precipitation_surface", 1.0, "mm day-1")
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, difference, a, b)
    assert exc.value.code == 2
    assert "edit-coords --rename OLD=NEW" in capsys.readouterr().err
