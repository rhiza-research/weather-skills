"""Correctness tests for submit-feedback."""

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def submit_feedback():
    return load_skill("submit-feedback", "build_url").submit_feedback


def test_build_url_prints_github_link(capsys, submit_feedback):
    run_skill(submit_feedback, "--title", "Plot bug", "--body", "Heatmap axis labels overlap.")

    out = capsys.readouterr().out
    assert "github.com/rhiza-research/weather-skills/issues/new" in out
    assert "title=Plot%20bug" in out


def test_overlong_body_exits_1(submit_feedback):
    body = "x" * 10000
    with pytest.raises(SystemExit) as exc:
        run_skill(submit_feedback, "--title", "Long body", "--body", body)
    assert exc.value.code == 1


def test_repo_flag_targets_allow_listed_repo(capsys, submit_feedback):
    run_skill(submit_feedback, "--title", "t", "--body", "b", "--repo", "weather-skills-plotting")
    assert "github.com/rhiza-research/weather-skills-plotting/issues/new" in capsys.readouterr().out


def test_repo_flag_rejects_other_repos(submit_feedback):
    with pytest.raises(SystemExit) as exc:
        run_skill(submit_feedback, "--title", "t", "--body", "b", "--repo", "someone/else")
    assert exc.value.code == 2


@pytest.mark.parametrize("body", ["", "  \n "])
def test_empty_body_exits_2(submit_feedback, body):
    # Old code printed a link to a blank issue and exited 0.
    with pytest.raises(SystemExit) as exc:
        run_skill(submit_feedback, "--title", "t", "--body", body)
    assert exc.value.code == 2


def test_invalid_utf8_argument_exits_2(capsys, submit_feedback):
    # Invalid UTF-8 argv bytes arrive as lone surrogates; old code raised a
    # UnicodeEncodeError traceback (exit 1) from quote().
    with pytest.raises(SystemExit) as exc:
        run_skill(submit_feedback, "--title", "t", "--body", "x\udcff")
    assert exc.value.code == 2
    assert "UTF-8" in capsys.readouterr().err


def test_body_file_directory_is_reported_as_not_a_file(capsys, submit_feedback, tmp_path):
    with pytest.raises(SystemExit) as exc:
        run_skill(submit_feedback, "--title", "t", "--body-file", str(tmp_path))
    assert exc.value.code == 2
    assert "not an existing file" in capsys.readouterr().err


def test_output_holds_only_the_title_and_body(capsys, submit_feedback):
    # Privacy: the link carries nothing beyond what the caller wrote.
    run_skill(submit_feedback, "--title", "T", "--body", "B")
    out = capsys.readouterr().out.strip()
    assert out == "https://github.com/rhiza-research/weather-skills/issues/new?title=T&body=B"
