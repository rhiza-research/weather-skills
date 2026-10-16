"""Correctness tests for downscale."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.standard_utils import normalize_latlon_coords
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def downscale():
    return load_skill("downscale", "downscale").downscale


def test_downscale_linear_factor2(tmp_path, downscale):
    src = write_zarr(
        make_gridded(lats=(1.0, 2.0), lons=(10.0, 11.0)),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "out.zarr"

    run_skill(
        downscale,
        "-i",
        str(src),
        "-o",
        str(out),
        "--algorithm",
        "linear-interpolation",
        "-f",
        "2",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["latitude"] > 2
    assert ds.sizes["longitude"] > 2
    assert units_equal(ds["precip"].attrs.get("units"), "mm day-1")
    assert load_history(out)[-1]["skill"] == "downscale"


def test_downscale_requires_one_target(tmp_path, downscale):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            downscale,
            "-i",
            str(src),
            "-o",
            str(out),
            "--algorithm",
            "linear-interpolation",
        )
    assert exc.value.code == 2


def test_downscale_rejects_coarser_reference(tmp_path, downscale):
    fine = write_zarr(
        make_gridded(lats=(0.0, 1.0, 2.0), lons=(10.0, 11.0, 12.0), fill=1.0),
        tmp_path / "fine.zarr",
    )
    coarse = write_zarr(
        make_gridded(lats=(0.0, 2.0), lons=(10.0, 12.0), fill=1.0),
        tmp_path / "coarse.zarr",
    )
    with pytest.raises(SystemExit) as exc:
        run_skill(
            downscale,
            "-i",
            str(fine),
            "-o",
            str(tmp_path / "out.zarr"),
            "--algorithm",
            "linear-interpolation",
            "--reference-grid",
            str(coarse),
        )
    assert exc.value.code == 2


def test_downscale_reference_grid_allows_lateral_shift(tmp_path, downscale):
    """Same 0.05° spacing, half-cell lon offset — do not bounce to coarsen."""
    import numpy as np

    src = write_zarr(
        make_gridded(
            lats=(0.00, 0.05, 0.10, 0.15),
            lons=(10.00, 10.05, 10.10, 10.15),
            fill=1.0,
        ),
        tmp_path / "src.zarr",
    )
    ref = write_zarr(
        make_gridded(
            lats=(0.00, 0.05, 0.10, 0.15),
            lons=(10.025, 10.075, 10.125, 10.175),
            fill=2.0,
        ),
        tmp_path / "ref.zarr",
    )
    out = tmp_path / "out.zarr"
    run_skill(
        downscale,
        "-i",
        str(src),
        "-o",
        str(out),
        "--algorithm",
        "linear-interpolation",
        "--reference-grid",
        str(ref),
    )
    result = xr.open_zarr(out, consolidated=True)
    expected = normalize_latlon_coords(xr.open_zarr(ref, consolidated=True))
    assert np.array_equal(result["latitude"].values, expected["latitude"].values)
    assert np.array_equal(result["longitude"].values, expected["longitude"].values)


def test_downscale_reference_grid_allows_near_equal_mixed_axes(tmp_path, downscale):
    """Lat slightly finer, lon slightly coarser — must not ping-pong to coarsen."""
    import numpy as np

    src = write_zarr(
        make_gridded(
            lats=tuple(np.arange(0.0, 0.20, 0.05)),
            lons=tuple(np.arange(10.0, 10.20, 0.05)),
            fill=1.0,
        ),
        tmp_path / "src.zarr",
    )
    ref = write_zarr(
        make_gridded(
            lats=tuple(np.arange(0.0, 0.20, 0.0499)),
            lons=tuple(np.arange(10.0, 10.20, 0.0501)),
            fill=2.0,
        ),
        tmp_path / "ref.zarr",
    )
    out = tmp_path / "out.zarr"
    run_skill(
        downscale,
        "-i",
        str(src),
        "-o",
        str(out),
        "--algorithm",
        "linear-interpolation",
        "--reference-grid",
        str(ref),
    )
    assert Path(out).exists()


def _clim(n, scale, name="precip"):
    """Climatology sample: value k*scale at time k, the same in every cell."""
    import numpy as np

    ds = make_gridded(n_time=n, lats=(1.0, 2.0), lons=(10.0, 11.0), name=name)
    ds[name].values[:] = (np.arange(n) * scale)[:, None, None]
    return ds


def test_qq_with_model_climatology_keeps_forecast_signal(tmp_path, downscale, capsys):
    """A wet forecast stays wet: F_ref^-1(F_model(x)), not the reference climatology."""
    import numpy as np

    fc = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0)).isel(time=0, drop=True)
    fc = fc.expand_dims(number=[0, 1]).copy(deep=True)
    fc["precip"].values[:] = np.array([2.0, 9.0])[:, None, None]  # 20th / 90th pct of model
    src = write_zarr(fc, tmp_path / "fc.zarr")
    model = write_zarr(_clim(10, 1.0), tmp_path / "model.zarr")  # model 0..9
    ref = write_zarr(_clim(10, 2.0), tmp_path / "ref.zarr")  # obs 0..18: model is half as wet
    out = tmp_path / "out.zarr"

    run_skill(
        downscale, "-i", str(src), "-o", str(out), "--algorithm", "q-q", "-f", "1",
        "--qq-reference", str(ref), "--qq-model", str(model),
    )  # fmt: skip

    res = xr.open_zarr(out)["precip"].isel(latitude=0, longitude=0)
    assert res.values == pytest.approx([4.0, 18.0])
    # The skill's own no-model warning, not any library warning (zarr prints its own).
    assert "input is its own model climatology" not in capsys.readouterr().err


def test_qq_without_model_warns(tmp_path, downscale, capsys):
    src = write_zarr(_clim(10, 1.0), tmp_path / "in.zarr")
    ref = write_zarr(_clim(10, 2.0), tmp_path / "ref.zarr")
    run_skill(
        downscale, "-i", str(src), "-o", str(tmp_path / "out.zarr"), "--algorithm", "q-q",
        "-f", "1", "--qq-reference", str(ref),
    )  # fmt: skip
    assert "input is its own model climatology" in capsys.readouterr().err


@pytest.mark.parametrize("case", ["name", "sample_dim"])
def test_qq_mismatch_errors(tmp_path, downscale, capsys, case):
    src = write_zarr(_clim(10, 1.0), tmp_path / "in.zarr")
    ref_ds = _clim(10, 2.0, name="tp") if case == "name" else _clim(1, 2.0).isel(time=0)
    ref = write_zarr(ref_ds, tmp_path / "ref.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            downscale, "-i", str(src), "-o", str(tmp_path / "out.zarr"), "--algorithm",
            "q-q", "-f", "1", "--qq-reference", str(ref),
        )  # fmt: skip
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert ("rename" in err) if case == "name" else ("sample dim" in err)


def test_qq_dry_forecast_stays_dry_with_tied_zeros(tmp_path, downscale):
    """Model dry 60% of the time, obs 40%: a 0 mm forecast must not map to observed rain."""
    import numpy as np

    fc = make_gridded(n_time=1, lats=(1.0, 2.0), lons=(10.0, 11.0), fill=0.0)
    src = write_zarr(fc, tmp_path / "fc.zarr")
    model, ref = _clim(10, 1.0), _clim(10, 1.0)
    model["precip"].values[:] = np.array([0, 0, 0, 0, 0, 0, 1, 2, 3, 4.0])[:, None, None]
    ref["precip"].values[:] = np.array([0, 0, 0, 0, 1, 2, 3, 4, 5, 6.0])[:, None, None]
    out = tmp_path / "out.zarr"
    run_skill(
        downscale, "-i", str(src), "-o", str(out), "--algorithm", "q-q", "-f", "1",
        "--qq-reference", str(write_zarr(ref, tmp_path / "ref.zarr")),
        "--qq-model", str(write_zarr(model, tmp_path / "model.zarr")),
    )  # fmt: skip
    assert (xr.open_zarr(out)["precip"].values == 0.0).all()


def test_qq_reference_in_other_units_errors(tmp_path, downscale, capsys):
    src = write_zarr(_clim(10, 1.0), tmp_path / "in.zarr")
    ref_ds = _clim(10, 2.0)
    ref_ds["precip"].attrs["units"] = "m day-1"
    with pytest.raises(SystemExit) as exc:
        run_skill(
            downscale, "-i", str(src), "-o", str(tmp_path / "out.zarr"), "--algorithm", "q-q",
            "-f", "1", "--qq-reference", str(write_zarr(ref_ds, tmp_path / "ref.zarr")),
        )  # fmt: skip
    assert exc.value.code == 2
    assert "unit-convert" in capsys.readouterr().err


def test_reference_grid_trimmed_and_own_dim_names(tmp_path, downscale):
    import numpy as np

    src = write_zarr(
        make_gridded(lats=(0.0, 1.0, 2.0), lons=(10.0, 11.0, 12.0)), tmp_path / "in.zarr"
    )
    lat, lon = np.arange(-10, 10.01, 0.5), np.arange(0, 20.01, 0.5)  # far larger than the input
    ref = xr.Dataset(coords={"lat": ("lat", lat), "lon": ("lon", lon)})
    ref["mask"] = (("lat", "lon"), np.ones((lat.size, lon.size)))
    out = tmp_path / "out.zarr"
    run_skill(
        downscale, "-i", str(src), "-o", str(out), "--algorithm", "linear-interpolation",
        "--reference-grid", str(write_zarr(ref, tmp_path / "ref.zarr")),
    )  # fmt: skip
    res = xr.open_zarr(out)
    assert res["latitude"].values.min() >= -0.5 and res["latitude"].values.max() <= 2.5
    assert res["longitude"].values.min() >= 9.5 and res["longitude"].values.max() <= 12.5


def _float32_grid(path, fill_offset=0.0):
    """CHIRPS-like 0.05° grid stored as float32, so its spacing is not exactly 0.05."""
    import numpy as np

    lats = (-4.675 + 0.05 * np.arange(204)).astype(np.float32)
    lons = (33.925 + 0.05 * np.arange(3)).astype(np.float32)
    ds = make_gridded(n_time=3, lats=tuple(lats.tolist()), lons=tuple(lons.tolist()))
    # Values change across the grid, so shifted output points would show.
    ds["precip"].values[:] = (
        np.arange(3)[:, None, None]
        + 0.1 * np.arange(204)[None, :, None]
        + 0.01 * np.arange(3)[None, None, :]
        + 1.0
        + fill_offset
    )
    ds = ds.assign_coords(latitude=lats, longitude=lons)
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    return write_zarr(ds, path)


def test_downscale_factor_one_keeps_the_float32_grid(tmp_path, downscale):
    import numpy as np

    src = _float32_grid(tmp_path / "src.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        downscale, "-i", str(src), "-o", str(out), "--algorithm", "linear-interpolation", "-f", "1"
    )
    a, b = xr.open_zarr(src), xr.open_zarr(out)
    assert np.array_equal(a["latitude"].values, b["latitude"].values)
    assert np.array_equal(a["longitude"].values, b["longitude"].values)
    np.testing.assert_allclose(b["precip"].values, a["precip"].values, rtol=0, atol=1e-5)


def test_downscale_factor_two_keeps_the_input_points(tmp_path, downscale):
    import numpy as np

    src = _float32_grid(tmp_path / "src.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        downscale, "-i", str(src), "-o", str(out), "--algorithm", "linear-interpolation", "-f", "2"
    )
    a, b = xr.open_zarr(src), xr.open_zarr(out)
    assert b.sizes["latitude"] == 2 * 204 - 1 and b.sizes["longitude"] == 2 * 3 - 1
    # Every second output point is an input point, with the input's value.
    np.testing.assert_allclose(b["latitude"].values[::2], a["latitude"].values, atol=1e-6)
    np.testing.assert_allclose(b["longitude"].values[::2], a["longitude"].values, atol=1e-6)
    np.testing.assert_allclose(
        b["precip"].values[:, ::2, ::2], a["precip"].values, rtol=0, atol=1e-5
    )
    # The points between lie halfway, evenly spaced to the far edge.
    np.testing.assert_allclose(np.diff(b["latitude"].values), 0.025, atol=1e-5)
    mid = (a["precip"].values[:, :-1, :] + a["precip"].values[:, 1:, :]) / 2
    np.testing.assert_allclose(b["precip"].values[:, 1::2, ::2], mid, rtol=0, atol=1e-5)


def test_downscale_qq_factor_one_accepts_reference_on_input_grid(tmp_path, downscale):
    import numpy as np

    src = _float32_grid(tmp_path / "src.zarr")
    ref = _float32_grid(tmp_path / "ref.zarr", fill_offset=10.0)
    out = tmp_path / "out.zarr"
    run_skill(
        downscale,
        "-i",
        str(src),
        "-o",
        str(out),
        "--algorithm",
        "q-q",
        "-f",
        "1",
        "--qq-reference",
        str(ref),
    )
    ds = xr.open_zarr(out)
    np.testing.assert_allclose(ds["precip"].isel(latitude=0, longitude=0).values, [11, 12, 13])


@pytest.mark.parametrize("factor", ["1", "2"])
def test_downscale_refuses_zero_spacing_grid(tmp_path, downscale, factor, capsys):
    src = write_zarr(make_gridded(lats=(0.0, 0.0, 0.0), lons=(10.0, 11.0)), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            downscale,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--algorithm",
            "linear-interpolation",
            "-f",
            factor,
        )
    assert exc.value.code == 2
    assert "spacing is zero" in capsys.readouterr().err
