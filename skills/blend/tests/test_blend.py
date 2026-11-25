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


def _forecast(tmp_path, name, fill, weeks=2, lats=(1.0, 2.0, 3.0)):
    ds = make_gridded(n_time=weeks, lats=lats, fill=fill)
    ds["precip"].attrs.update(units="mm", aggregation_period="7 day")
    return str(write_zarr(ds, tmp_path / f"{name}_weekly_totals_init2026-01-01.zarr"))


def _run(blend, tmp_path, inputs, *args):
    out = tmp_path / "out.zarr"
    run_skill(blend, *[a for i in inputs for a in ("-i", i)], *args, "-o", str(out))
    return xr.open_zarr(out)


def _exit_code(blend, tmp_path, inputs, *args):
    with pytest.raises(SystemExit) as exc:
        _run(blend, tmp_path, inputs, *args)
    return exc.value.code


def test_equal_weights_by_default(tmp_path, blend):
    ab = [_forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)]

    ds = _run(blend, tmp_path, ab)

    assert [str(t)[:10] for t in ds["time"].values] == ["2026-01-01"]
    np.testing.assert_allclose(ds["precip"].values, 3.0)
    assert json.loads(ds.attrs["blend_weights"]) == {"week1": {"a": 0.5, "b": 0.5}}
    assert ds["precip"].attrs["long_name"].endswith(" blend")
    assert load_history(tmp_path / "out.zarr")[-1]["skill"] == "blend"


def test_weights_per_week_inline_or_from_file(tmp_path, blend):
    ab = [_forecast(tmp_path, "a", 2.0, weeks=3), _forecast(tmp_path, "b", 4.0, weeks=3)]
    weights = '{"week1": {"a": 1, "b": 0}, "week3": {"a": 3, "b": 1}}'
    file = tmp_path / "blend_weights.json"
    file.write_text(weights)

    for given in (weights, str(file)):
        ds = _run(blend, tmp_path, ab, "--lead", "1", "3", "--weights", given)

        assert [str(t)[:10] for t in ds["time"].values] == ["2026-01-01", "2026-01-03"]
        np.testing.assert_allclose(ds["precip"].isel(time=0).values, 2.0)
        np.testing.assert_allclose(ds["precip"].isel(time=1).values, 2.5)
        assert json.loads(ds.attrs["blend_leads"]) == [1, 3]
        assert json.loads(ds.attrs["blend_weights"])["week3"] == {"a": 0.75, "b": 0.25}


def test_nan_where_any_weighted_forecast_is_nan(tmp_path, blend):
    ds = make_gridded(n_time=1, fill=2.0)
    ds["precip"][0, 0, 0] = np.nan
    ab = [str(write_zarr(ds, tmp_path / "a.zarr")), _forecast(tmp_path, "b", 4.0, weeks=1)]

    blended = _run(blend, tmp_path, ab)["precip"].values[0]
    assert np.isnan(blended[0, 0])
    assert np.isfinite(blended).sum() == blended.size - 1
    # A forecast with weight 0 does not blank the blend. a has no aggregation period,
    # so its leads are lead1, ...
    zero_a = _run(blend, tmp_path, ab, "--weights", '{"lead1": {"b": 1}}')
    np.testing.assert_allclose(zero_a["precip"].values, 4.0)


def test_a_forecast_short_of_a_later_week(tmp_path, blend):
    abc = [
        _forecast(tmp_path, "a", 2.0),
        _forecast(tmp_path, "b", 4.0),
        _forecast(tmp_path, "c", 6.0, weeks=1),
    ]

    ds = _run(blend, tmp_path, abc, "--lead", "1", "2", "--keep-members")

    np.testing.assert_allclose(ds["precip"].isel(time=0).values, 4.0)
    np.testing.assert_allclose(ds["precip"].isel(time=1).values, 3.0)
    assert json.loads(ds.attrs["blend_left_out"]) == {"week1": [], "week2": ["c"]}
    assert json.loads(ds.attrs["blend_weights"])["week2"] == {"a": 0.5, "b": 0.5, "c": 0.0}
    assert list(ds["model"].values) == ["a", "b", "c"]
    assert np.isnan(ds["precip_members"].isel(time=1).sel(model="c").values).all()


def test_labels(tmp_path, blend):
    ab = [_forecast(tmp_path, "a", 2.0), _forecast(tmp_path, "b", 4.0)]

    ds = _run(blend, tmp_path, ab, "--label", "gefs", "--label", "ecmwf", "--keep-members")

    assert list(ds["model"].values) == ["gefs", "ecmwf"]
    np.testing.assert_allclose(ds["precip_members"].sel(model="ecmwf").values, 4.0)


@pytest.mark.parametrize(
    "args",
    [
        ["--weights", '{"week1": {"a": 0.5, "z": 0.5}}'],  # z is not an input
        ["--weights", '{"week2": {"a": 1}}'],  # no week1
        ["--weights", '{"week1": {"a": -1, "b": 2}}'],  # negative
        ["--weights", "a=0.5,b=0.5"],  # not JSON
        ["--lead", "2", "--weights", '{"week2": {"a": 0.5, "c": 0.5}}'],  # c has no week 2
        ["--lead", "5"],  # no input reaches week 5
        ["--label", "a"],  # one label for three inputs
    ],
    ids=["unknown", "missing-week", "negative", "not-json", "no-lead", "too-far", "labels"],
)
def test_bad_arguments_exit_2(tmp_path, blend, args):
    abc = [
        _forecast(tmp_path, "a", 2.0),
        _forecast(tmp_path, "b", 4.0),
        _forecast(tmp_path, "c", 1.0, weeks=1),
    ]
    assert _exit_code(blend, tmp_path, abc, *args) == 2


def test_needs_two_inputs(tmp_path, blend):
    assert _exit_code(blend, tmp_path, [_forecast(tmp_path, "a", 2.0)]) == 2


def test_other_grid_without_regrid_skills_says_where_to_get_them(
    tmp_path, module, blend, monkeypatch, capsys
):
    inputs = [
        _forecast(tmp_path, "a", 2.0),
        _forecast(tmp_path, "b", 4.0),
        _forecast(tmp_path, "fine", 1.0, lats=(1.0, 1.5, 2.0, 2.5, 3.0)),
    ]
    monkeypatch.setattr(module, "_SKILLS", tmp_path / "no-skills")

    assert _exit_code(blend, tmp_path, inputs) == 2
    err = capsys.readouterr().err
    assert "coarsen" in err and "weather-skills/weather-skills" in err


def test_label_from_file_name(module):
    assert module._label("/x/windborne_wm6_weekly_totals_init2026-11-02.zarr") == "windborne_wm6"
    assert module._label("gefs_init2026-11-02.zarr/") == "gefs"
    assert module._label("plain.zarr") == "plain"
