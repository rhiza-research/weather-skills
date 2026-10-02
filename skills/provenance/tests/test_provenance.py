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
