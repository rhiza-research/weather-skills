"""Guards documented in SKILL.md, restored after #108 removed them from difference.py.

Each test pins one promise from skills/difference/SKILL.md, so the code and the text cannot drift
apart silently again.
"""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr


@pytest.fixture(scope="module")
def difference():
    return load_skill("difference", "difference").difference


def _with_units(ds, units="mm day-1"):
    for v in ds.data_vars:
        ds[v].attrs["units"] = units
    return ds


def _inputs(tmp_path, a, b):
    return (
        write_zarr(a, tmp_path / "a.zarr"),
        write_zarr(b, tmp_path / "b.zarr"),
        tmp_path / "out.zarr",
    )


def test_no_shared_data_variable_exits_nonzero(tmp_path, difference):
    a, b, out = _inputs(
        tmp_path,
        _with_units(make_gridded(name="precip")),
        _with_units(make_gridded(name="t2m"), "K"),
    )
    with pytest.raises(SystemExit) as exc:
        run_skill(difference, "-i", str(a), "-i", str(b), "-o", str(out))
    assert exc.value.code == 2
    assert not out.exists()


def test_variable_missing_from_an_input_lists_each_inputs_variables(tmp_path, difference, capsys):
    a, b, out = _inputs(tmp_path, _with_units(make_gridded()), _with_units(make_gridded()))
    with pytest.raises(SystemExit) as exc:
        run_skill(difference, "-i", str(a), "-i", str(b), "-o", str(out), "-v", "t2m")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "t2m" in err and "precip" in err
    assert not out.exists()


def test_differing_compatible_units_are_converted(tmp_path, difference):
    # Pins the behaviour SKILL.md now documents: B is converted to A's units before subtracting
    # (5 mm/day - 2 mm/hour = 5 - 48 = -43 mm/day), rather than raw values being mixed.
    a, b, out = _inputs(
        tmp_path,
        _with_units(make_gridded(fill=5.0), "mm day-1"),
        _with_units(make_gridded(fill=2.0), "mm hour-1"),
    )
    run_skill(difference, "-i", str(a), "-i", str(b), "-o", str(out))
    ds = xr.open_zarr(out, consolidated=True)
    np.testing.assert_allclose(ds["precip"].values, -43.0)


def test_equal_size_coordinateless_dim_warns_positional(tmp_path, difference, capsys):
    a = xr.Dataset({"precip": (("gauge",), np.array([1.0, 2.0, 3.0]), {"units": "mm day-1"})})
    b = xr.Dataset({"precip": (("gauge",), np.array([0.5, 0.5, 0.5]), {"units": "mm day-1"})})
    pa, pb, out = _inputs(tmp_path, a, b)
    run_skill(difference, "-i", str(pa), "-i", str(pb), "-o", str(out))
    assert "positionally" in capsys.readouterr().err
    np.testing.assert_allclose(xr.open_zarr(out)["precip"].values, [0.5, 1.5, 2.5])


def test_coordinateless_dim_size_mismatch_exits_naming_the_dim(tmp_path, difference, capsys):
    a = xr.Dataset({"precip": (("gauge",), np.array([1.0, 2.0, 3.0]), {"units": "mm day-1"})})
    b = xr.Dataset({"precip": (("gauge",), np.array([0.5, 0.5]), {"units": "mm day-1"})})
    pa, pb, out = _inputs(tmp_path, a, b)
    with pytest.raises(SystemExit) as exc:
        run_skill(difference, "-i", str(pa), "-i", str(pb), "-o", str(out))
    assert exc.value.code == 2
    assert "gauge" in capsys.readouterr().err


def test_no_overlap_after_alignment_exits_without_output(tmp_path, difference, capsys):
    a, b, out = _inputs(
        tmp_path,
        _with_units(make_gridded(lats=(1.0, 2.0))),
        _with_units(make_gridded(lats=(5.0, 6.0))),
    )
    with pytest.raises(SystemExit) as exc:
        run_skill(difference, "-i", str(a), "-i", str(b), "-o", str(out))
    assert exc.value.code == 2
    assert "no overlapping" in capsys.readouterr().err
    assert not out.exists()


def test_unselected_variables_are_dropped_with_a_note(tmp_path, difference, capsys):
    a = _with_units(make_gridded())
    a["extra"] = a["precip"] * 2
    a, b, out = _inputs(tmp_path, a, _with_units(make_gridded()))
    run_skill(difference, "-i", str(a), "-i", str(b), "-o", str(out))
    assert "dropping data variable(s) ['extra']" in capsys.readouterr().err
    assert list(xr.open_zarr(out).data_vars) == ["precip"]
