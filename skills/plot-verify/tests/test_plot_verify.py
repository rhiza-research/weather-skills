"""Correctness tests for plot-verify."""

import json
from pathlib import Path

import pytest
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core.errors import UsageError
from weather_skills_core.provenance import load_figure_history


@pytest.fixture(scope="module")
def plot_mod():
    return load_skill("plot-verify", "plot_verify")


@pytest.fixture(scope="module")
def plot_fn(plot_mod):
    return plot_mod.plot_verify


@pytest.fixture(scope="module")
def verify_fn():
    return load_skill("verify", "verify").verify


def _week(*, event_at, fill=0.0, name="precip"):
    ds = make_gridded(n_time=1, fill=fill, name=name)
    for i, j in event_at:
        ds[name].values[0, i, j] = 5.0
    return ds


def _run_verify(verify_fn, fc_path, obs_path, out_path, *, metric="hits", threshold="1"):
    args = ["--forecast", str(fc_path), "--obs", str(obs_path), "-o", str(out_path)]
    if metric != "hits":
        args.extend(["--metric", metric])
    if threshold != "1":
        args.extend(["--threshold", threshold])
    run_skill(verify_fn, *args)


def test_four_leads_write_png_and_stamp_history(tmp_path, plot_fn, verify_fn, capsys):
    obs = write_zarr(_week(event_at=[(0, 0), (1, 0)]), tmp_path / "obs.zarr")
    forecasts = []
    verify_paths = []
    for k, cells in zip((1, 2, 3, 4), ([(0, 0)], [(0, 0), (1, 0)], [(1, 0)], []), strict=True):
        fc_path = write_zarr(_week(event_at=cells), tmp_path / f"w{k}.zarr")
        forecasts.append(fc_path)
        vpath = tmp_path / f"v{k}.zarr"
        _run_verify(verify_fn, fc_path, obs, vpath)
        verify_paths.append(vpath)
    out = tmp_path / "verify.png"

    args = ["--obs", str(obs)]
    for fc_path, vpath in zip(forecasts, verify_paths, strict=True):
        args.extend(["--forecast", str(fc_path), "--verify", str(vpath)])
    args.extend(["-o", str(out), "--title", "Obs week"])
    run_skill(plot_fn, *args)

    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history is not None
    assert history[-1]["skill"] == "plot-verify"
    printed = capsys.readouterr().out
    leads = [ln.split("  ", 1)[0] for ln in printed.splitlines() if "-week lead" in ln]
    assert leads == ["1-week lead", "2-week lead", "3-week lead", "4-week lead"]
    assert "hit rate" in printed


def test_metric_bias_writes_png(tmp_path, plot_fn, verify_fn, capsys):
    obs = write_zarr(_week(event_at=[(0, 0)], fill=1.0), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[(0, 0)], fill=3.0), tmp_path / "fc.zarr")
    vpath = tmp_path / "bias.zarr"
    _run_verify(verify_fn, fc, obs, vpath, metric="bias")
    out = tmp_path / "verify_bias.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(fc),
        "--verify",
        str(vpath),
        "-o",
        str(out),
    )
    assert Path(out).exists()
    assert "bias" in capsys.readouterr().out


def test_replot_from_spec(tmp_path, plot_fn, verify_fn):
    obs = write_zarr(_week(event_at=[(0, 0)], fill=1.0), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[(0, 0)], fill=3.0), tmp_path / "fc.zarr")
    vpath = tmp_path / "bias.zarr"
    _run_verify(verify_fn, fc, obs, vpath, metric="bias")
    first = tmp_path / "verify_bias.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(fc),
        "--verify",
        str(vpath),
        "-o",
        str(first),
        "--title",
        "Original",
        "--dump-spec",
        str(tmp_path / "verify_bias.plot.json"),
    )
    spec_path = tmp_path / "verify_bias.plot.json"
    data = json.loads(spec_path.read_text())
    assert any(item.get("id") == "verify1" for item in data["inputs"])
    data["title"] = "Edited"
    spec_path.write_text(json.dumps(data))
    second = tmp_path / "verify_bias2.png"
    run_skill(plot_fn, "--spec", str(spec_path), "-o", str(second))
    assert second.is_file() and second.stat().st_size > 0
    history = load_figure_history(second)
    assert history[-1]["skill"] == "plot-verify"
    basenames = [item["basename"] for item in history[-1]["input"]]
    assert "obs.zarr" in basenames and "fc.zarr" in basenames and "bias.zarr" in basenames


def test_patch_flag_merges_into_spec(tmp_path, plot_fn, verify_fn):
    obs = write_zarr(_week(event_at=[(0, 0)], fill=1.0), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[(0, 0)], fill=3.0), tmp_path / "fc.zarr")
    vpath = tmp_path / "bias.zarr"
    _run_verify(verify_fn, fc, obs, vpath, metric="bias")
    out = tmp_path / "verify_bias.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(fc),
        "--verify",
        str(vpath),
        "-o",
        str(out),
        "--patch",
        '{"title": "Patched"}',
        "--dump-spec",
        str(tmp_path / "verify_bias.plot.json"),
    )
    spec = json.loads((tmp_path / "verify_bias.plot.json").read_text())
    assert spec["title"].startswith("Patched")
    assert "patch" not in spec


def test_error_scale_bias_white_at_zero():
    import numpy as np
    import xarray as xr
    from weather_skills_core.plot.maps import error_scale

    da = xr.DataArray(np.array([[-2.0, 0.0], [0.5, 3.0]]), name="bias")
    scale = error_scale(da, "bias")
    assert scale["name"] == "verify_bias"
    assert scale["cmin"] == -scale["cmax"]
    assert scale["cmax"] == 3.0
    colors = scale["colors"]
    mid = colors[len(colors) // 2]
    assert mid.lower() in {"#ffffff", "rgb(255,255,255)", "white"}


def test_error_scale_mae_white_at_zero():
    import numpy as np
    import xarray as xr
    from weather_skills_core.plot.maps import error_scale

    da = xr.DataArray(np.array([[0.0, 1.0], [2.0, 4.0]]), name="mae")
    scale = error_scale(da, "mae")
    assert scale["name"] == "verify_mae"
    assert scale["cmin"] == 0.0
    assert scale["cmax"] == 4.0
    assert scale["colors"][0].lower() in {"#ffffff", "white"}


def test_verify_count_mismatch_is_refused(tmp_path, plot_fn):
    obs = write_zarr(_week(event_at=[]), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[]), tmp_path / "fc.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_fn,
            "--obs",
            str(obs),
            "--forecast",
            str(fc),
            "-o",
            str(tmp_path / "out.png"),
        )
    assert exc.value.code == 2


def test_parse_figsize(plot_mod):
    import argparse

    assert plot_mod.parse_figsize("14,8") == (14.0, 8.0)
    with pytest.raises(argparse.ArgumentTypeError, match="W,H"):
        plot_mod.parse_figsize("wide")


def test_row_labels_use_weather_skills_source(plot_mod):
    obs = _week(event_at=[])
    obs.attrs["weather_skills_source"] = "chirps"
    a = _week(event_at=[])
    a.attrs["weather_skills_source"] = "ecmwf-s2s"
    b = _week(event_at=[])
    b.attrs["weather_skills_source"] = "ecmwf-s2s"
    assert plot_mod._row_labels(obs, [a, b], "hits") == ("CHIRPS", "ECMWF S2S", "Hits")
    assert plot_mod._row_labels(obs, [a, b], "bias") == ("CHIRPS", "ECMWF S2S", "Bias")


def test_row_labels_fallback_without_source(plot_mod):
    obs = _week(event_at=[])
    fc = _week(event_at=[])
    assert plot_mod._row_labels(obs, [fc]) == ("Observation", "Forecast", "Hits")


def test_row_labels_explicit_overrides(plot_mod):
    obs = _week(event_at=[])
    a = _week(event_at=[])
    b = _week(event_at=[])
    assert plot_mod._row_labels(obs, [a, b], "hits", labels=["Obs custom", "Fc A", "Fc B"]) == (
        "Obs custom",
        "Fc A / Fc B",
        "Hits",
    )


def test_label_count_mismatch_is_refused(plot_mod):
    obs = _week(event_at=[])
    fc = _week(event_at=[])
    with pytest.raises(UsageError, match="expected 2 --label"):
        plot_mod._row_labels(obs, [fc], labels=["Only obs"])


def test_custom_lead_labels(tmp_path, plot_fn, verify_fn, capsys):
    obs = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "obs.zarr")
    a = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "a.zarr")
    b = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "b.zarr")
    va = tmp_path / "va.zarr"
    vb = tmp_path / "vb.zarr"
    _run_verify(verify_fn, a, obs, va)
    _run_verify(verify_fn, b, obs, vb)
    out = tmp_path / "verify.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(a),
        "--verify",
        str(va),
        "--forecast",
        str(b),
        "--verify",
        str(vb),
        "--lead",
        "W1",
        "--lead",
        "W2",
        "-o",
        str(out),
    )
    assert "W1" in capsys.readouterr().out
    assert Path(out).exists()


def test_lead_count_mismatch_is_refused(tmp_path, plot_fn, verify_fn):
    obs = write_zarr(_week(event_at=[]), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[]), tmp_path / "fc.zarr")
    vpath = tmp_path / "v.zarr"
    _run_verify(verify_fn, fc, obs, vpath)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_fn,
            "--obs",
            str(obs),
            "--forecast",
            str(fc),
            "--verify",
            str(vpath),
            "--lead",
            "W1",
            "--lead",
            "W2",
            "-o",
            str(tmp_path / "out.png"),
        )
    assert exc.value.code == 2


def test_step_forecast_without_time_is_refused(tmp_path, plot_fn, verify_fn):
    obs = write_zarr(make_gridded(n_time=1, fill=5.0), tmp_path / "obs.zarr")
    fc = write_zarr(make_forecast(n_step=2, fill=5.0), tmp_path / "fc.zarr")
    vpath = tmp_path / "v.zarr"
    with pytest.raises(SystemExit):
        _run_verify(verify_fn, fc, obs, vpath)


def test_obs_finer_grid_is_refused(tmp_path, plot_fn, verify_fn):
    fc = write_zarr(
        make_gridded(n_time=1, fill=5.0, lats=(1.0, 2.0), lons=(10.0, 11.0)),
        tmp_path / "fc.zarr",
    )
    obs = write_zarr(
        make_gridded(n_time=1, fill=5.0, lats=(1.0, 1.5, 2.0), lons=(10.0, 10.5, 11.0)),
        tmp_path / "obs.zarr",
    )
    vpath = tmp_path / "v.zarr"
    with pytest.raises(SystemExit):
        _run_verify(verify_fn, fc, obs, vpath)


def test_multi_time_obs_is_refused(tmp_path, plot_fn, verify_fn):
    obs = write_zarr(make_gridded(n_time=2, fill=5.0), tmp_path / "obs.zarr")
    fc = write_zarr(make_gridded(n_time=1, fill=5.0), tmp_path / "fc.zarr")
    vpath = tmp_path / "v.zarr"
    _run_verify(verify_fn, fc, obs, vpath)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_fn,
            "--obs",
            str(obs),
            "--forecast",
            str(fc),
            "--verify",
            str(vpath),
            "-o",
            str(tmp_path / "out.png"),
        )
    assert exc.value.code == 2


def test_bbox_slices_before_draw(tmp_path, plot_fn, verify_fn):
    obs = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "obs.zarr")
    fc = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "fc.zarr")
    vpath = tmp_path / "v.zarr"
    _run_verify(verify_fn, fc, obs, vpath)
    out = tmp_path / "verify.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(fc),
        "--verify",
        str(vpath),
        "-o",
        str(out),
        "--bbox",
        "3/9/0/12",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_order_week1_first_sorts_week_labels(plot_mod):
    leads, forecasts, verifies, labels = plot_mod._order_week1_first(
        ["Week 4 (init Sep 1)", "Week 1 (init Sep 22)"],
        ["w4", "w1"],
        ["v4", "v1"],
        ["Obs", "GEFS w4", "GEFS w1"],
    )
    assert leads == ["Week 1 (init Sep 22)", "Week 4 (init Sep 1)"]
    assert forecasts == ["w1", "w4"]
    assert verifies == ["v1", "v4"]
    assert labels == ["Obs", "GEFS w1", "GEFS w4"]


def test_order_week1_first_keeps_custom_labels(plot_mod):
    leads, forecasts, verifies, labels = plot_mod._order_week1_first(
        ["W1", "W2"],
        ["a", "b"],
        ["va", "vb"],
        None,
    )
    assert leads == ["W1", "W2"]
    assert forecasts == ["a", "b"]
    assert verifies == ["va", "vb"]
    assert labels is None


def test_week_labels_print_week1_to_week4(tmp_path, plot_fn, verify_fn, capsys):
    obs = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "obs.zarr")
    w4 = write_zarr(_week(event_at=[]), tmp_path / "w4.zarr")
    w1 = write_zarr(_week(event_at=[(0, 0)]), tmp_path / "w1.zarr")
    v4 = tmp_path / "v4.zarr"
    v1 = tmp_path / "v1.zarr"
    _run_verify(verify_fn, w4, obs, v4)
    _run_verify(verify_fn, w1, obs, v1)
    out = tmp_path / "verify.png"
    run_skill(
        plot_fn,
        "--obs",
        str(obs),
        "--forecast",
        str(w4),
        "--verify",
        str(v4),
        "--forecast",
        str(w1),
        "--verify",
        str(v1),
        "--lead",
        "Week 4",
        "--lead",
        "Week 1",
        "-o",
        str(out),
    )
    printed = capsys.readouterr().out
    lines = [ln.split("  ", 1)[0] for ln in printed.splitlines() if ln.startswith("Week ")]
    assert lines == ["Week 1", "Week 4"]
