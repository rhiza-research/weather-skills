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


def test_unit_convert_to_units(tmp_path, unit_convert):
    src = write_zarr(make_gridded(fill=24.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "mm hr-1")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["precip"].values == pytest.approx(1.0)
    assert units_equal(ds["precip"].attrs["units"], "mm hr-1")


def _run_exit_code(unit_convert, *args):
    with pytest.raises(SystemExit) as exc:
        run_skill(unit_convert, *args)
    return exc.value.code


def test_unit_convert_unknown_variable_is_usage_error(tmp_path, unit_convert):
    # Was an uncaught KeyError traceback (exit 1) with --to-units.
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    args = ("-i", str(src), "-o", str(out), "-v", "nope", "--to-units", "mm hr-1")
    assert _run_exit_code(unit_convert, *args) == 2
    assert not out.exists()


def test_unit_convert_unparseable_units_is_usage_error(tmp_path, unit_convert):
    # Was an uncaught pint UndefinedUnitError (exit 1).
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    args = ("-i", str(src), "-o", str(out), "--to-units", "not_a_unit")
    assert _run_exit_code(unit_convert, *args) == 2
    assert not out.exists()


def test_unit_convert_standard_name_with_to_standard_is_refused(tmp_path, unit_convert):
    # Was silently ignored.
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    args = ("-i", str(src), "-o", str(out), "--to-standard", "--standard-name", "x")
    assert _run_exit_code(unit_convert, *args) == 2
    assert not out.exists()


def test_unit_convert_to_standard_unrecognized_variable_is_refused(tmp_path, unit_convert):
    # Was a silent no-op copy: -v sst with --to-standard left kelvin in place.
    ds = make_gridded(name="sst", fill=300.0)
    ds["sst"].attrs = {"units": "K", "standard_name": "sea_surface_temperature"}
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    args = ("-i", str(src), "-o", str(out), "--to-standard", "-v", "sst")
    assert _run_exit_code(unit_convert, *args) == 2
    assert not out.exists()


def test_unit_convert_to_standard_reports_unrecognized(tmp_path, unit_convert, capsys):
    ds = make_gridded(name="t2m", fill=300.0)
    ds["t2m"].attrs = {"units": "K", "standard_name": "air_temperature"}
    ds["sst"] = ds["t2m"].copy()
    ds["sst"].attrs = {"units": "K", "standard_name": "sea_surface_temperature"}
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-standard")

    result = xr.open_zarr(out, consolidated=True)
    assert float(result["t2m"].max()) == pytest.approx(26.85)
    assert float(result["sst"].max()) == pytest.approx(300.0)
    assert "sst" in capsys.readouterr().err


def test_unit_convert_writes_standard_spelling(tmp_path, unit_convert):
    # Was written verbatim: "millimeter / day", "degC".
    ds = make_gridded(fill=1.0 / 86400.0)
    ds["precip"].attrs.update(units="kg m-2 s-1", standard_name="precipitation_flux")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "millimeter / day")

    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["units"] == "mm day-1"
    assert result["precip"].attrs["standard_name"] == "lwe_precipitation_rate"
    assert float(result["precip"].max()) == pytest.approx(1.0)


def test_unit_convert_flux_to_hourly_rate_renames_standard_name(tmp_path, unit_convert):
    # Was precipitation_flux (canonical kg m-2 s-1) on an mm hr-1 depth rate:
    # only the exact spellings mm day-1 / mm/day were mapped.
    ds = make_gridded(fill=1.0 / 3600.0)
    ds["precip"].attrs.update(units="kg m-2 s-1", standard_name="precipitation_flux")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "mm hr-1")

    result = xr.open_zarr(out, consolidated=True)
    assert result["precip"].attrs["standard_name"] == "lwe_precipitation_rate"
    assert float(result["precip"].max()) == pytest.approx(1.0)


def test_unit_convert_non_precip_rate_gets_no_precip_name(tmp_path, unit_convert):
    # Was lwe_precipitation_rate on any unnamed variable converted to mm day-1.
    ds = make_gridded(name="evap", fill=2.0)
    ds["evap"].attrs = {"units": "mm/day"}
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "mm day-1")

    assert "standard_name" not in xr.open_zarr(out, consolidated=True)["evap"].attrs


def test_unit_convert_temperatures_and_anomalies(tmp_path, unit_convert):
    ds = make_gridded(name="t2m", fill=300.0).astype("float32")
    ds["t2m"].attrs = {"units": "K", "standard_name": "air_temperature"}
    ds["t2m_anom"] = ds["t2m"] * 0 + 9.0
    ds["t2m_anom"].attrs = {"units": "delta_degF"}
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-standard")

    result = xr.open_zarr(out, consolidated=True)
    assert float(result["t2m"].max()) == pytest.approx(26.85, abs=1e-4)
    assert result["t2m"].dtype == "float32"
    # A difference stays a difference: 9 delta_degF is 5 delta_degC, no 273 offset.
    assert float(result["t2m_anom"].max()) == pytest.approx(5.0)
    assert result["t2m_anom"].attrs["units"] == "delta_degC"
    # An absolute target on a difference is refused, not offset.
    args = ("-i", str(src), "-o", str(tmp_path / "o2.zarr"), "-v", "t2m_anom")
    assert _run_exit_code(unit_convert, *args, "--to-units", "degC") == 2


def test_unit_convert_mass_amount_to_depth(tmp_path, unit_convert):
    ds = make_gridded(name="tp", fill=5.0).astype("float32")
    ds["tp"].attrs = {"units": "kg m-2", "standard_name": "precipitation_amount"}
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "mm")

    result = xr.open_zarr(out, consolidated=True)
    assert float(result["tp"].max()) == pytest.approx(5.0)
    assert result["tp"].dtype == "float32"
    assert result["tp"].attrs["standard_name"] == "lwe_thickness_of_precipitation_amount"


def test_unit_convert_refuses_rate_to_amount(tmp_path, unit_convert, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    args = ("-i", str(src), "-o", str(out), "--to-units", "mm")
    assert _run_exit_code(unit_convert, *args) == 2
    assert "convert-to-totals" in capsys.readouterr().err
    assert not out.exists()
