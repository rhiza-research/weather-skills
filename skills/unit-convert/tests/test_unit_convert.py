"""Correctness tests for unit-convert."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def unit_convert():
    return load_skill("unit-convert", "unit-convert").unit_convert


def test_unit_convert_to_standard(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-standard")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert units_equal(ds["precip"].attrs["units"], "mm day-1")
    assert load_history(out)[-1]["skill"] == "unit-convert"


def test_unit_convert_to_units_reads_quantified_units(tmp_path, unit_convert):
    # Input has a units attr on disk; the decorator moves it into the pint
    # quantity, so the skill must not look for it only in attrs.
    src = write_zarr(make_gridded(fill=24.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "mm/hour")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["precip"].attrs["units"] == "mm/hour"
    assert float(ds["precip"].max()) == pytest.approx(1.0)
    assert load_history(out)[-1]["args"]["to_units"] == "mm/hour"


def test_unit_convert_to_units_temperature_offset(tmp_path, unit_convert):
    ds = make_gridded(name="t2m", fill=300.0)
    ds["t2m"].attrs.update(units="K", standard_name="air_temperature")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        unit_convert, "-i", str(src), "-o", str(out), "-v", "t2m", "--to-units", "degree_Celsius"
    )

    result = xr.open_zarr(out, consolidated=True)
    assert result["t2m"].attrs["units"] == "degree_Celsius"
    assert result["t2m"].attrs["standard_name"] == "air_temperature"
    assert float(result["t2m"].max()) == pytest.approx(26.85)


def test_unit_convert_to_units_mass_flux_to_depth_rate(tmp_path, unit_convert):
    ds = make_gridded(fill=1.0 / 86400.0)
    ds["precip"].attrs.update(units="kg m-2 s-1", standard_name="precipitation_flux")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "mm day-1")

    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["units"] == "mm day-1"
    assert result["precip"].attrs["standard_name"] == "lwe_precipitation_rate"
    assert float(result["precip"].max()) == pytest.approx(1.0)


def test_unit_convert_to_units_same_units_is_noop(tmp_path, unit_convert):
    src = write_zarr(make_gridded(fill=3.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "mm/day")

    ds = xr.open_zarr(out, consolidated=True)
    assert units_equal(ds["precip"].attrs["units"], "mm/day")
    assert float(ds["precip"].max()) == pytest.approx(3.0)


def test_unit_convert_to_units_unitless_variable_is_refused(tmp_path, unit_convert):
    ds = make_gridded(name="index")
    ds["index"].attrs.pop("units")
    ds["index"].attrs.pop("standard_name")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "mm")
    assert exc.value.code == 2


def test_unit_convert_rejects_both_targets(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            unit_convert,
            "-i",
            str(src),
            "-o",
            str(out),
            "--to-standard",
            "--to-units",
            "mm day-1",
        )
    assert exc.value.code == 2


def test_unit_convert_requires_target(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(unit_convert, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2
