"""Correctness tests for blend."""

import json

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def module():
    return load_skill("blend", "blend")


@pytest.fixture(scope="module")
def blend(module):
    return module.blend


def _forecast(tmp_path, name, fill, n_time=2, lats=(1.0, 2.0, 3.0)):
    ds = make_gridded(n_time=n_time, lats=lats, fill=fill)
    ds["precip"].attrs.update(units="mm", aggregation_period="7 day")
    return str(write_zarr(ds, tmp_path / f"{name}_weekly_totals_init2026-01-01.zarr"))


def test_equal_weights_by_default(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)
    out = tmp_path / "out.zarr"

    run_skill(blend, "-i", a, "-i", b, "-o", str(out))

    ds = xr.open_zarr(out)
    assert ds["precip"].sizes["time"] == 1
    assert str(ds["time"].values[0])[:10] == "2026-01-01"
    np.testing.assert_allclose(ds["precip"].values, 3.0)
    assert json.loads(ds.attrs["blend_weights"]) == {"week1": {"a": 0.5, "b": 0.5}}
    assert ds["precip"].attrs["long_name"].endswith(" blend")
    assert load_history(out)[-1]["skill"] == "blend"


def test_weight_pairs_and_lead(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)
    out = tmp_path / "out.zarr"

    run_skill(blend, "-i", a, "-i", b, "--lead", "2", "--weights", "a=0.75,b=0.25", "-o", str(out))

    ds = xr.open_zarr(out)
    assert str(ds["time"].values[0])[:10] == "2026-01-02"
    np.testing.assert_allclose(ds["precip"].values, 2.5)
    assert json.loads(ds.attrs["blend_leads"]) == [2]


def test_weights_from_interface_file(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)
    weights = tmp_path / "blend_weights_2026-01-01_lead1.json"
    weights.write_text('{"a":0.25,"b":0.75}')
    out = tmp_path / "out.zarr"

    run_skill(blend, "-i", a, "-i", b, "--weights", str(weights), "-o", str(out))

    np.testing.assert_allclose(xr.open_zarr(out)["precip"].values, 3.5)


def test_weights_are_scaled_to_one(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)
    out = tmp_path / "out.zarr"

    run_skill(blend, "-i", a, "-i", b, "--weights", '{"a": 3, "b": 1}', "-o", str(out))

    ds = xr.open_zarr(out)
    np.testing.assert_allclose(ds["precip"].values, 2.5)
    assert json.loads(ds.attrs["blend_weights"]) == {"week1": {"a": 0.75, "b": 0.25}}


def test_nan_where_any_weighted_forecast_is_nan(tmp_path, blend):
    ds = make_gridded(n_time=1, fill=2.0)
    ds["precip"][0, 0, 0] = np.nan
    a = str(write_zarr(ds, tmp_path / "a.zarr"))
    b = _forecast(tmp_path, "b", 4.0, n_time=1)
    out, out0 = tmp_path / "out.zarr", tmp_path / "out0.zarr"

    run_skill(blend, "-i", a, "-i", b, "-o", str(out))
    run_skill(blend, "-i", a, "-i", b, "--weights", "a=0,b=1", "-o", str(out0))

    blended = xr.open_zarr(out)["precip"].values[0]
    assert np.isnan(blended[0, 0])
    assert np.isfinite(blended).sum() == blended.size - 1
    # A forecast with weight 0 does not blank the blend.
    np.testing.assert_allclose(xr.open_zarr(out0)["precip"].values, 4.0)


def test_forecast_without_the_lead_is_left_out(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)
    c = _forecast(tmp_path, "c", 100.0, n_time=1)
    out = tmp_path / "out.zarr"

    run_skill(blend, "-i", a, "-i", b, "-i", c, "--lead", "2", "-o", str(out))

    ds = xr.open_zarr(out)
    np.testing.assert_allclose(ds["precip"].values, 3.0)
    assert json.loads(ds.attrs["blend_left_out"]) == {"week2": ["c"]}


def test_several_leads_with_weights_per_lead(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0, n_time=3), _forecast(tmp_path, "b", 4.0, n_time=3)
    weights = '{"week1": {"a": 1, "b": 0}, "week3": {"a": 0.5, "b": 0.5}}'
    out = tmp_path / "out.zarr"

    run_skill(blend, "-i", a, "-i", b, "--lead", "1,3", "--weights", weights, "-o", str(out))

    ds = xr.open_zarr(out)
    assert [str(t)[:10] for t in ds["time"].values] == ["2026-01-01", "2026-01-03"]
    np.testing.assert_allclose(ds["precip"].isel(time=0).values, 2.0)
    np.testing.assert_allclose(ds["precip"].isel(time=1).values, 3.0)
    assert json.loads(ds.attrs["blend_leads"]) == [1, 3]
    assert json.loads(ds.attrs["blend_weights"]) == {
        "week1": {"a": 1.0, "b": 0.0},
        "week3": {"a": 0.5, "b": 0.5},
    }


def test_per_lead_keys_may_be_numbers_and_non_weekly_leads_are_lead_n(tmp_path, blend):
    a = str(write_zarr(make_gridded(n_time=2, fill=2.0), tmp_path / "a.zarr"))
    b = str(write_zarr(make_gridded(n_time=2, fill=4.0), tmp_path / "b.zarr"))
    weights = '{"1": {"a": 1, "b": 0}, "lead2": {"a": 0, "b": 1}}'
    out = tmp_path / "out.zarr"

    run_skill(blend, "-i", a, "-i", b, "--lead", "1-2", "--weights", weights, "-o", str(out))

    ds = xr.open_zarr(out)
    np.testing.assert_allclose(ds["precip"].isel(time=0).values, 2.0)
    np.testing.assert_allclose(ds["precip"].isel(time=1).values, 4.0)
    assert list(json.loads(ds.attrs["blend_weights"])) == ["lead1", "lead2"]


def test_flat_weights_apply_to_every_lead(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0, n_time=3), _forecast(tmp_path, "b", 4.0, n_time=3)
    out = tmp_path / "out.zarr"

    run_skill(
        blend, "-i", a, "-i", b, "--lead", "1-3", "--weights", "a=0.75,b=0.25", "-o", str(out)
    )

    np.testing.assert_allclose(xr.open_zarr(out)["precip"].values, 2.5)


def test_a_forecast_short_of_a_later_lead(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)
    c = _forecast(tmp_path, "c", 6.0, n_time=1)
    out = tmp_path / "out.zarr"

    run_skill(blend, "-i", a, "-i", b, "-i", c, "--lead", "1-2", "--keep-members", "-o", str(out))

    ds = xr.open_zarr(out)
    np.testing.assert_allclose(ds["precip"].isel(time=0).values, 4.0)
    np.testing.assert_allclose(ds["precip"].isel(time=1).values, 3.0)
    assert json.loads(ds.attrs["blend_left_out"]) == {"week1": [], "week2": ["c"]}
    assert json.loads(ds.attrs["blend_weights"])["week2"] == {"a": 0.5, "b": 0.5, "c": 0.0}
    assert np.isnan(ds["precip_members"].isel(time=1).sel(model="c").values).all()


@pytest.mark.parametrize("lead", ["0", "3-1", "week1"])
def test_bad_lead_exits_2(tmp_path, blend, lead):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)

    with pytest.raises(SystemExit) as exc:
        run_skill(blend, "-i", a, "-i", b, "--lead", lead, "-o", str(tmp_path / "o.zarr"))
    assert exc.value.code == 2


def test_per_lead_weights_must_cover_every_lead(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)

    with pytest.raises(SystemExit) as exc:
        run_skill(
            blend, "-i", a, "-i", b, "--lead", "1-2", "--weights", '{"week1": {"a": 1, "b": 0}}',
            "-o", str(tmp_path / "o.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2


def test_keep_members_and_labels(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)
    out = tmp_path / "out.zarr"

    run_skill(
        blend, "-i", a, "-i", b, "--label", "gefs", "--label", "ecmwf", "--keep-members",
        "-o", str(out),
    )  # fmt: skip

    members = xr.open_zarr(out)["precip_members"]
    assert list(members["model"].values) == ["gefs", "ecmwf"]
    np.testing.assert_allclose(members.sel(model="ecmwf").values, 4.0)


@pytest.mark.parametrize(
    "weights",
    ["a=0.5,z=0.5", "a=1", "a=-1,b=2", "not-a-file.json"],
    ids=["unknown-name", "missing-name", "negative", "missing-file"],
)
def test_bad_weights_exit_2(tmp_path, blend, weights):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)

    with pytest.raises(SystemExit) as exc:
        run_skill(blend, "-i", a, "-i", b, "--weights", weights, "-o", str(tmp_path / "o.zarr"))
    assert exc.value.code == 2


def test_weight_on_a_forecast_without_the_lead_exits_2(tmp_path, blend):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)
    c = _forecast(tmp_path, "c", 1.0, n_time=1)

    with pytest.raises(SystemExit) as exc:
        run_skill(
            blend, "-i", a, "-i", b, "-i", c, "--lead", "2", "--weights", "a=0.4,b=0.4,c=0.2",
            "-o", str(tmp_path / "o.zarr"),
        )  # fmt: skip
    assert exc.value.code == 2


def test_needs_two_inputs(tmp_path, blend):
    with pytest.raises(SystemExit) as exc:
        run_skill(blend, "-i", _forecast(tmp_path, "a", 2.0), "-o", str(tmp_path / "o.zarr"))
    assert exc.value.code == 2


def test_other_grid_without_regrid_skills_says_where_to_get_them(
    tmp_path, module, blend, monkeypatch, capsys
):
    a, b = _forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)
    fine = _forecast(tmp_path, "fine", 1.0, lats=(1.0, 1.5, 2.0, 2.5, 3.0))
    monkeypatch.setattr(module, "_SKILLS", tmp_path / "no-skills")

    with pytest.raises(SystemExit) as exc:
        run_skill(blend, "-i", a, "-i", b, "-i", fine, "-o", str(tmp_path / "o.zarr"))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "coarsen" in err and "weather-skills/weather-skills" in err


def test_default_label(module):
    label = module._default_label
    assert label("/x/windborne_wm6_weekly_totals_init2026-11-02.zarr") == "windborne_wm6"
    assert label("gefs_init2026-11-02.zarr/") == "gefs"
    assert label("plain.zarr") == "plain"
