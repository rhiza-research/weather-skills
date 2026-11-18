"""Correctness tests for provenance."""

import json
import re

import pytest
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import stamp_zarr


@pytest.fixture(scope="module")
def clip_region():
    return load_skill("clip-region", "clip").clip_region


@pytest.fixture(scope="module")
def provenance():
    return load_skill("provenance", "provenance").provenance


def _stamped_zarr(tmp_path, clip_region):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(clip_region, "-i", str(src), "-o", str(out), "--bbox", "3/10/0/13")
    return out


def _stamp_history(path, history):
    ds = make_gridded()
    stamp_zarr(ds, history)
    ds.to_zarr(path, mode="w", consolidated=True)
    return path


def test_check_valid_history(tmp_path, clip_region, provenance, capsys):
    out = _stamped_zarr(tmp_path, clip_region)

    run_skill(provenance, "-i", str(out), "--check")

    captured = capsys.readouterr().out
    assert "valid weather_skills_history" in captured


def test_human_format_lists_clip_region(tmp_path, clip_region, provenance, capsys):
    out = _stamped_zarr(tmp_path, clip_region)

    run_skill(provenance, "-i", str(out), "--format", "human")

    captured = capsys.readouterr().out
    assert "clip-region" in captured


def test_human_format_shows_join_and_commit(tmp_path, provenance, capsys):
    history = [
        {
            "skill": "difference",
            "version": "0.0.2",
            "commit": "abc123def4567890",
            "repo": "https://github.com/rhiza-research/weather-skills",
            "args": {},
            "input": [
                {
                    "basename": "a.zarr",
                    "hash": "aa",
                    "history": [
                        {
                            "skill": "chirps-fetch",
                            "version": "0.0.2",
                            "commit": "111111111111",
                            "args": {"bbox": "1/2/3/4"},
                            "input": None,
                        }
                    ],
                },
                {
                    "basename": "b.zarr",
                    "hash": "bb",
                    "history": [
                        {
                            "skill": "dynamical-fetch",
                            "version": "0.0.2",
                            "commit": "222222222222",
                            "args": {"dataset": "noaa-gefs-forecast-35-day"},
                            "input": None,
                        }
                    ],
                },
            ],
        }
    ]
    out = _stamp_history(tmp_path / "join.zarr", history)

    run_skill(provenance, "-i", str(out), "--format", "human")

    captured = capsys.readouterr().out
    assert "difference (v0.0.2 @abc123def456)" in captured
    assert "input branch a (a.zarr)" in captured
    assert "chirps-fetch (v0.0.2 @111111111111)" in captured
    assert "input branch b (b.zarr)" in captured
    assert "dynamical-fetch (v0.0.2 @222222222222)" in captured


def test_script_pins_commit_and_reproduces_join(tmp_path, provenance, capsys):
    history = [
        {
            "skill": "concat",
            "version": "0.0.2",
            "commit": "deadbeefcafebabe",
            "repo": "https://github.com/example/weather-skills",
            "args": {"dim": "number"},
            "input": [
                {
                    "basename": "a.zarr",
                    "hash": "aa",
                    "history": [
                        {
                            "skill": "chirps-fetch",
                            "version": "0.0.2",
                            "commit": "aaaaaaaaaaaa",
                            "repo": "https://github.com/example/weather-skills",
                            "args": {},
                            "input": None,
                        }
                    ],
                },
                {
                    "basename": "b.zarr",
                    "hash": "bb",
                    "history": [
                        {
                            "skill": "dynamical-fetch",
                            "version": "0.0.2",
                            "commit": "bbbbbbbbbbbb",
                            "args": {},
                            "input": None,
                        }
                    ],
                },
            ],
        }
    ]
    out = _stamp_history(tmp_path / "join.zarr", history)

    run_skill(provenance, "-i", str(out), "--format", "script")

    captured = capsys.readouterr().out
    assert "uvx --from git+https://github.com/example/weather-skills@aaaaaaaaaaaa" in captured
    assert "uvx --from git+https://github.com/example/weather-skills@deadbeefcafebabe" in captured
    assert "chirps-fetch" in captured
    assert "dynamical-fetch" in captured
    assert "concat" in captured
    assert "--dim number" in captured


def test_check_accepts_stamped_join(tmp_path, provenance, capsys):
    history = [
        {
            "skill": "concat",
            "version": "0.0.2",
            "commit": "abc123",
            "args": {},
            "input": [
                {
                    "basename": "a.zarr",
                    "hash": "aa",
                    "history": [
                        {
                            "skill": "chirps-fetch",
                            "version": "0.0.2",
                            "args": {},
                            "input": None,
                        }
                    ],
                },
                {
                    "basename": "b.zarr",
                    "hash": "bb",
                    "history": [],
                },
            ],
        }
    ]
    out = _stamp_history(tmp_path / "join.zarr", history)

    run_skill(provenance, "-i", str(out), "--check")

    captured = capsys.readouterr().out
    assert "valid weather_skills_history" in captured


def _fetch(skill, **extra):
    return {"skill": skill, "version": "0.0.2", "args": {}, "input": None, **extra}


def _script(provenance, path, capsys):
    run_skill(provenance, "-i", str(path), "--format", "script")
    return capsys.readouterr().out


def test_script_unpinned_step_runs_from_dev(tmp_path, provenance, capsys):
    out = _stamp_history(tmp_path / "f.zarr", [_fetch("chirps-fetch")])

    captured = _script(provenance, out, capsys)

    assert "git+https://github.com/rhiza-research/weather-skills@dev weather-skills" in captured


def test_script_runs_other_repo_skill_from_checkout(tmp_path, provenance, capsys):
    repo = "https://github.com/example-org/other-skills"
    history = [
        _fetch("chirps-fetch", commit="aaaa"),
        {
            "skill": "make-map",
            "version": "0.0.2",
            "commit": "bbbb",
            "repo": repo,
            "args": {},
            "input": {"basename": "a.zarr", "hash": "aa"},
        },
    ]
    out = _stamp_history(tmp_path / "f.zarr", history)

    captured = _script(provenance, out, capsys)

    assert "_checkout() {" in captured
    assert f'uv run --script "$(_checkout {repo} bbbb)"/skills/make-map/scripts/*.py' in captured
    assert f"{repo}@bbbb weather-skills" not in captured


def test_script_emits_structured_args(tmp_path, provenance, capsys):
    args = {"spec": {"title": "Kenya's rain"}, "variable": [["tp", "t2m"]]}
    out = _stamp_history(tmp_path / "f.zarr", [_fetch("ecmwf-fetch", args=args)])

    captured = _script(provenance, out, capsys)

    assert """--spec '{"title": "Kenya'"'"'s rain"}'""" in captured
    assert "--variable tp t2m" in captured


def test_script_verify_join_uses_forecast_and_obs(tmp_path, provenance, capsys):
    parents = [
        {"basename": n, "hash": "h", "history": [_fetch("chirps-fetch")]}
        for n in ("fc.zarr", "obs.zarr")
    ]
    history = [{"skill": "verify", "version": "0.0.2", "args": {}, "input": parents}]
    out = _stamp_history(tmp_path / "v.zarr", history)

    captured = _script(provenance, out, capsys)

    assert "verify --forecast a.zarr --obs b.zarr --output v.zarr" in captured


def test_script_reproduces_join_before_later_step(tmp_path, clip_region, provenance, capsys):
    # difference, then a single-input step: the join is not the last entry.
    a = write_zarr(make_gridded(lats=(-3.0, -2.0, -1.0), fill=2.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(lats=(-3.0, -2.0, -1.0), fill=1.0), tmp_path / "b.zarr")
    run_skill(clip_region, "-i", str(a), "-o", str(tmp_path / "ca.zarr"), "--bbox", "-1/10/-3/13")
    run_skill(clip_region, "-i", str(b), "-o", str(tmp_path / "cb.zarr"), "--bbox", "-1/10/-3/13")
    difference = load_skill("difference", "difference").difference
    diff = tmp_path / "diff.zarr"
    run_skill(
        difference,
        "-i",
        str(tmp_path / "ca.zarr"),
        "-i",
        str(tmp_path / "cb.zarr"),
        "-o",
        str(diff),
    )
    final = tmp_path / "final.zarr"
    run_skill(clip_region, "-i", str(diff), "-o", str(final), "--bbox", "-1/10/-2/12")
    capsys.readouterr()

    captured = _script(provenance, final, capsys)

    # a.zarr / b.zarr have no history: read from the path core recorded, or
    # <UPSTREAM> for a core that leaves inputs out of args.
    for out in ("a", "b"):
        assert re.search(
            rf"clip-region --bbox -1/10/-3/13 --input \S+ --output {out}\.zarr", captured
        )
    assert "difference --input a.zarr --input b.zarr --output step1.zarr" in captured
    assert "clip-region --bbox -1/10/-2/12 --input step1.zarr --output final.zarr" in captured


def test_malformed_entries_do_not_traceback(tmp_path, provenance, capsys):
    history = [
        {"skill": "clip-region", "version": "1", "args": ["x"], "input": None},
        {"skill": "concat", "version": "1", "args": {}, "input": ["a.zarr", "b.zarr"]},
    ]
    out = _stamp_history(tmp_path / "bad.zarr", history)

    run_skill(provenance, "-i", str(out), "--format", "human")
    human = capsys.readouterr().out
    captured = _script(provenance, out, capsys)

    assert "args: (malformed: not an object)" in human
    assert "input: [?, ?]" in human
    assert "concat --input step1.zarr --input '?'" in captured


def test_reads_html_and_jpeg_figures(tmp_path, provenance, capsys):
    from PIL import Image
    from weather_skills_core.provenance import stamp_figure

    history = [_fetch("chirps-fetch", args={"bbox": "1/2/3/4", "workers": None})]
    html = tmp_path / "fig.html"
    html.write_text("<html><head></head><body></body></html>")
    jpg = tmp_path / "fig.jpg"
    Image.new("RGB", (200, 200)).save(jpg)
    for path in (html, jpg):
        stamp_figure(path, history)

        run_skill(provenance, "-i", str(path), "--format", "human")
        captured = capsys.readouterr().out
        run_skill(provenance, "-i", str(path), "--check")

        assert "chirps-fetch" in captured
        assert "bbox='1/2/3/4'" in captured
        assert "workers" not in captured
        assert "valid weather_skills_history" in capsys.readouterr().out


def test_reads_gif_pdf_and_svg_figures(tmp_path, provenance, capsys):
    from PIL import Image
    from weather_skills_core.provenance import stamp_figure

    history = [_fetch("chirps-fetch", args={"bbox": "1/2/3/4", "title": "Rain <b>—</b> & wk 1"})]
    gif = tmp_path / "fig.gif"
    Image.new("RGB", (200, 200)).save(gif)
    pdf = tmp_path / "fig.pdf"
    Image.new("RGB", (200, 200)).save(pdf)
    svg = tmp_path / "fig.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">'
        '<rect width="200" height="200"/></svg>'
    )
    for path in (gif, pdf, svg):
        stamp_figure(path, history)

        run_skill(provenance, "-i", str(path), "--format", "json")
        assert json.loads(capsys.readouterr().out) == history
        run_skill(provenance, "-i", str(path), "--check")
        assert "valid weather_skills_history" in capsys.readouterr().out


def test_reads_geojson_and_table_histories(tmp_path, provenance, capsys):
    from weather_skills_core.provenance import stamp_figure

    history = [_fetch("chirps-fetch", args={"bbox": "1/2/3/4"})]
    geo = tmp_path / "region.geojson"
    geo.write_text('{"type": "FeatureCollection", "features": []}')
    table = tmp_path / "rain.csv"
    table.write_text("time,precip\n2026-01-01,1.0\n")
    for path in (geo, table):
        stamp_figure(path, history)
        run_skill(provenance, "-i", str(path), "--check")
        assert "valid weather_skills_history" in capsys.readouterr().out


def test_script_reruns_resolve_region_geojson(tmp_path, provenance, capsys):
    resolve_region = load_skill("resolve-region", "resolve").resolve_region
    geo = tmp_path / "io.geojson"
    run_skill(resolve_region, "Indian Ocean", "--geojson", str(geo))
    capsys.readouterr()

    captured = _script(provenance, geo, capsys)

    assert "resolve-region 'Indian Ocean' --pad 0.25 --geojson io.geojson" in captured
    assert "--code" not in captured


def _plot_step(input_field):
    # A skill with its own repeatable -i/--input PATH=NAME: core keeps it in args.
    args = {
        "input": ["/x/grid.zarr=G", "/x/coast.geojson=C"],
        "const": ["top=5"],
        "spec": {"data": [{"type": "heatmap", "z": "@G.precip"}]},
    }
    return {"skill": "plot", "version": "0.0.2", "args": args, "input": input_field}


def test_script_keeps_recorded_input_flags(tmp_path, provenance, capsys):
    step = _plot_step({"basename": "grid.zarr", "hash": "h"})
    out = _stamp_history(tmp_path / "rep.zarr", [step])

    captured = _script(provenance, out, capsys)

    spec = """'{"data": [{"type": "heatmap", "z": "@G.precip"}]}'"""
    assert (
        "plot --const top=5 --input /x/grid.zarr=G --input /x/coast.geojson=C "
        f"--spec {spec} --output rep.zarr"
    ) in captured
    assert "<UPSTREAM>" not in captured
    assert captured.count("--input") == 2


def test_script_links_recorded_input_to_upstream(tmp_path, provenance, capsys):
    history = [_fetch("chirps-fetch"), _plot_step({"basename": "grid.zarr", "hash": "h"})]
    out = _stamp_history(tmp_path / "rep.zarr", history)

    captured = _script(provenance, out, capsys)

    spec = """'{"data": [{"type": "heatmap", "z": "@G.precip"}]}'"""
    assert "chirps-fetch --output step1.zarr" in captured
    assert (
        "plot --const top=5 --input step1.zarr=G --input /x/coast.geojson=C "
        f"--spec {spec} --output rep.zarr"
    ) in captured
    assert captured.count("--input") == 2


def test_script_reads_imported_file_from_its_recorded_flag(tmp_path, provenance, capsys):
    # An imported CSV is recorded as the input and as --csv; it is read in place.
    step = {
        "skill": "csv-io",
        "version": "0.0.1",
        "args": {"csv": "/x/rain.csv"},
        "input": {"basename": "rain.csv", "hash": "h"},
    }
    history = [step, {**_fetch("drop"), "input": {"basename": "step1.zarr", "hash": "h"}}]
    out = _stamp_history(tmp_path / "rep.zarr", history)

    captured = _script(provenance, out, capsys)

    assert "csv-io --csv /x/rain.csv --output step1.zarr" in captured
    assert "drop --input step1.zarr --output rep.zarr" in captured
    assert "<UPSTREAM>" not in captured


def test_script_keeps_imported_join_files_at_recorded_paths(tmp_path, provenance, capsys):
    # Several imported NetCDF files record a join whose parents have no history.
    step = {
        "skill": "netcdf-io",
        "version": "0.0.1",
        "args": {"netcdf": ["/x/a.nc", "/x/b.nc"]},
        "input": [
            {"basename": "a.nc", "hash": "h", "history": []},
            {"basename": "b.nc", "hash": "h", "history": []},
        ],
    }
    out = _stamp_history(tmp_path / "rep.zarr", [step])

    captured = _script(provenance, out, capsys)

    assert "netcdf-io --netcdf /x/a.nc --netcdf /x/b.nc --output rep.zarr" in captured
    assert "--input" not in captured
    assert "yourself" not in captured


def test_script_relinks_every_input_named_in_args(tmp_path, provenance, capsys):
    # Core records each input's path in args (here --input and --reference-grid);
    # the replay swaps each for the file it makes and appends nothing.
    step = {
        "skill": "coarsen",
        "version": "0.0.1",
        "args": {
            "input": "work/chirps_dek.zarr",
            "method": "conservative",
            "reference_grid": "work/imerg_dek.zarr",
        },
        "input": [
            {"basename": "chirps_dek.zarr", "hash": "h", "history": [_fetch("chirps-fetch")]},
            {"basename": "imerg_dek.zarr", "hash": "h", "history": [_fetch("imerg-fetch")]},
        ],
    }
    later = {**_fetch("drop"), "args": {"input": "work/c.zarr"}}
    later["input"] = {"basename": "c.zarr", "hash": "h"}
    out = _stamp_history(tmp_path / "rep.zarr", [step, later])

    captured = _script(provenance, out, capsys)

    assert "chirps-fetch --output a.zarr" in captured
    assert "imerg-fetch --output b.zarr" in captured
    assert (
        "coarsen --input a.zarr --method conservative --reference-grid b.zarr --output step1.zarr"
    ) in captured
    assert "drop --input step1.zarr --output rep.zarr" in captured
    assert captured.count("--input") == 2
