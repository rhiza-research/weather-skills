"""Correctness tests for provenance."""

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
    # The default branch names its CLI `weather-skills`; `forecasting-skills` is on dev.
    out = _stamp_history(tmp_path / "f.zarr", [_fetch("chirps-fetch")])

    captured = _script(provenance, out, capsys)

    assert "git+https://github.com/rhiza-research/weather-skills@dev forecasting-skills" in captured


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
    assert f"{repo}@bbbb forecasting-skills" not in captured


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

    assert "clip-region --bbox -1/10/-3/13 --input '<UPSTREAM>' --output a.zarr" in captured
    assert "clip-region --bbox -1/10/-3/13 --input '<UPSTREAM>' --output b.zarr" in captured
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
