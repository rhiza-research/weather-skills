"""Correctness tests for blend-interface.

Building the page needs weather-skills-plotting, which the dev environment lacks,
so these cover the checks that run before it.
"""

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def module():
    return load_skill("blend-interface", "blend_interface")


@pytest.mark.parametrize(
    "args",
    [
        ["-o", "p.png"],  # not .html
        ["--anomaly", "a_anom.zarr", "-o", "p.html"],  # one anomaly for two inputs
    ],
    ids=["png", "anomaly-count"],
)
def test_bad_arguments_exit_2(tmp_path, module, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        run_skill(module.blend_interface, "-i", "a.zarr", "-i", "b.zarr", *args)
    assert exc.value.code == 2
