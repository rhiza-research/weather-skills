"""Correctness tests for blend-interface.

Building the page needs weather-skills-plotting, which the dev environment lacks,
so these cover what runs before it: argument checks, labels and the panel layout.
"""

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def module():
    return load_skill("blend-interface", "blend_interface")


def test_output_must_be_html(tmp_path, module):
    with pytest.raises(SystemExit) as exc:
        run_skill(
            module.blend_interface, "-i", "a.zarr", "-i", "b.zarr", "-o", str(tmp_path / "p.png")
        )
    assert exc.value.code == 2


def test_one_anomaly_per_input(tmp_path, module):
    with pytest.raises(SystemExit) as exc:
        run_skill(
            module.blend_interface, "-i", "a.zarr", "-i", "b.zarr", "--anomaly", "a_anom.zarr",
            "-o", str(tmp_path / "p.html"),
        )  # fmt: skip
    assert exc.value.code == 2


def test_missing_blend_skill_exits_2(tmp_path, module, monkeypatch):
    monkeypatch.setattr(module, "_BLEND", tmp_path / "no-blend.py")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            module.blend_interface, "-i", "a.zarr", "-i", "b.zarr", "-o", str(tmp_path / "p.html")
        )
    assert exc.value.code == 2


def test_labels_match_blend():
    blend = load_skill("blend", "blend")._default_label
    interface = load_skill("blend-interface", "blend_interface")._default_label
    for path in (
        "x/windborne_wm6_weekly_totals_init2026-11-02.zarr",
        "gefs_init2026.zarr",
        "a.zarr",
    ):
        assert interface(path) == blend(path)


@pytest.mark.parametrize("n_models", [2, 4, 8])
@pytest.mark.parametrize("n_leads", [1, 3])
def test_layout_is_a_row_per_lead_with_the_blend_last(module, n_models, n_leads):
    cols, small, blend = module._layout(n_models, n_leads)
    assert cols == n_models + 1
    assert len(small) == len(blend) == n_leads
    for r in range(n_leads):
        assert small[r] == list(range(r * cols, r * cols + n_models))
        assert blend[r] == r * cols + n_models
