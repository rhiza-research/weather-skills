"""Correctness tests for onset-date."""

import collections
import hashlib
import json
import tomllib
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr


@pytest.fixture(scope="module")
def onset_date():
    return load_skill("onset-date", "onset_date").onset_date


def _forecast_ds(values, name="tp", extra_var=None):
    """A single-gridpoint forecast (`step` lead-time dim) carrying `values`
    as its daily rainfall series, for exercising the onset search directly."""
    n = len(values)
    steps = np.array([np.timedelta64(d, "D") for d in range(1, n + 1)])
    data = np.asarray(values, dtype=np.float64).reshape(n, 1, 1)
    coords = {
        "time": np.datetime64("2026-01-01", "ns"),
        "step": steps,
        "latitude": [1.0],
        "longitude": [10.0],
    }
    data_vars = {name: (["step", "latitude", "longitude"], data)}
    if extra_var is not None:
        data_vars["mask"] = (["latitude", "longitude"], np.array([[1.0]]))
    ds = xr.Dataset(data_vars, coords=coords)
    ds[name].attrs.update(units="mm", long_name="Total precipitation")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["step"].attrs.update(
        standard_name="forecast_period", long_name="time since forecast_reference_time"
    )
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")
    return ds


def test_icpac_onset_detected(tmp_path, onset_date):
    # Days 0-2 (3-day wet spell) sum to 25mm > 20mm threshold; no dry spell
    # (>=7 consecutive days < 1mm) within the following 21 days.
    values = [10, 10, 5] + [2.0] * 27
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "ICPAC",
        "--wet-spell-thresh",
        "20",
        "--wet-spell-days",
        "3",
        "--dry-spell-thresh",
        "1",
        "--dry-spell-days",
        "7",
        "--search-days",
        "21",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert onset == np.timedelta64(1, "D")  # onset on the first day (step index 0)
    assert ds["onset_tp_date"].attrs["standard_name"] is None
    assert "ICPAC" in ds["onset_tp_date"].attrs["long_name"]
    assert "step" not in ds.dims


def test_icpac_onset_disqualified_by_dry_spell(tmp_path, onset_date):
    # Same qualifying wet spell, but followed immediately by an 8-day dry
    # run inside the 21-day search window -> onset must not fire here.
    values = [10, 10, 5] + [0.0] * 8 + [2.0] * 19
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "ICPAC",
        "--wet-spell-thresh",
        "20",
        "--wet-spell-days",
        "3",
        "--dry-spell-thresh",
        "1",
        "--dry-spell-days",
        "7",
        "--search-days",
        "21",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert onset != np.timedelta64(1, "D")


def test_icpac_all_dry_gives_nat(tmp_path, onset_date):
    values = [0.0] * 30
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "ICPAC",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert np.isnat(onset)


def test_chc_start_grow_season_onset(tmp_path, onset_date):
    # Days 0-9 (period1=10) sum to exactly 20mm; days 10-29 (period2=20)
    # sum to more than 20mm -> onset at day 0.
    values = [2.0] * 10 + [1.5] * 20
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "CHC_start_grow_season",
        "--period1-days",
        "10",
        "--period1-thresh",
        "20",
        "--period2-days",
        "20",
        "--period2-thresh",
        "20",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert onset == np.timedelta64(1, "D")
    assert "CHC_start_grow_season" in ds["onset_tp_date"].attrs["long_name"]


def test_chc_start_grow_season_fails_period2(tmp_path, onset_date):
    # period1 satisfied at day 0, but period2 total (20mm) never exceeds
    # its own threshold anywhere in the series -> NaT.
    values = [2.0] * 10 + [0.5] * 20
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "CHC_start_grow_season",
        "--period1-days",
        "10",
        "--period1-thresh",
        "20",
        "--period2-days",
        "20",
        "--period2-thresh",
        "20",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert np.isnat(onset)


def test_variable_selection_and_passthrough(tmp_path, onset_date):
    values = [10, 10, 5] + [2.0] * 27
    src = write_zarr(_forecast_ds(values, extra_var=True), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "ICPAC",
        "--variable",
        "tp",
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert "mask" in ds.data_vars
    assert ds["mask"].values[0, 0] == 1.0  # untouched passthrough


def test_nan_in_window_gives_nat(tmp_path, onset_date):
    values = [10.0, 10.0, np.nan] + [2.0] * 27
    src = write_zarr(_forecast_ds(values), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "ICPAC",
    )

    ds = xr.open_zarr(out, consolidated=True)
    onset = ds["onset_tp_date"].isel(latitude=0, longitude=0).values
    assert np.isnat(onset)


def test_requires_definition(tmp_path, onset_date):
    src = write_zarr(_forecast_ds([2.0] * 30), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(onset_date, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def test_output_variable_name_is_not_misclassified(tmp_path, onset_date):
    """Regression: a `{var}_onset_date`-style name (e.g. `tp_onset_date`)
    starts with the precip name-hint "tp", which weather_skills_core's
    variable classifier then treats as a standard "precip" kind requiring a
    `units` attr -- one this datetime/timedelta output can never carry (see
    the write-time CF-encoding note above). That silently broke reading this
    skill's own output back in as `--input` to any other skill. `onset_{var}_date`
    sandwiches `var` so it never sits at either end of the name."""
    from weather_skills_core.units import classify_variable

    src = write_zarr(_forecast_ds([10, 10, 5] + [2.0] * 27), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(onset_date, "-i", str(src), "-o", str(out), "--definition", "ICPAC")

    ds = xr.open_zarr(out, consolidated=True)
    assert "onset_tp_date" in ds.data_vars
    assert classify_variable("onset_tp_date") is None


# ---------------------------------------------------------------------------
# Moron_Robertson_2014
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def onset_module():
    return load_skill("onset-date", "onset_date")


def _run_mr(onset_date, tmp_path, values_or_ds, *extra, thresh="10"):
    ds = values_or_ds if isinstance(values_or_ds, xr.Dataset) else _forecast_ds(values_or_ds)
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    thresh_args = ("--mr-thresh", thresh) if thresh is not None else ()
    run_skill(
        onset_date,
        "-i",
        str(src),
        "-o",
        str(out),
        "--definition",
        "Moron_Robertson_2014",
        *thresh_args,
        *extra,
    )
    return xr.open_zarr(out, consolidated=True)


def _daily_ds(values, start="2026-05-01"):
    """Single-gridpoint daily series on an absolute `time` dim."""
    n = len(values)
    times = np.arange(np.datetime64(start), np.datetime64(start) + np.timedelta64(n, "D"))
    data = np.asarray(values, dtype=np.float64).reshape(n, 1, 1)
    ds = xr.Dataset(
        {"tp": (["time", "latitude", "longitude"], data)},
        coords={"time": times.astype("datetime64[ns]"), "latitude": [1.0], "longitude": [10.0]},
    )
    ds["tp"].attrs.update(units="mm", long_name="Total precipitation")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(standard_name="time", axis="T")
    return ds


def _onset(ds):
    return ds["onset_tp_date"].isel(latitude=0, longitude=0).values


def test_mr_clean_onset(tmp_path, onset_date):
    # 10 dry days, then a 5-day all-wet window of 3mm/day (15mm > 10mm), then
    # 2mm/day: every 10-day window in the 30-day follow-up totals 20mm >= 5mm.
    values = [0.0] * 10 + [3.0] * 5 + [2.0] * 30
    ds = _run_mr(onset_date, tmp_path, values)
    assert _onset(ds) == np.timedelta64(11, "D")  # step index 10
    assert "Moron_Robertson_2014" in ds["onset_tp_date"].attrs["long_name"]
    assert ds["onset_tp_date"].attrs["standard_name"] is None
    assert "step" not in ds.dims


def test_mr_trigger_threshold_is_strict(tmp_path, onset_date):
    # A 5-day window of 2mm totals exactly 10mm: not > 10mm, so no onset.
    assert np.isnat(_onset(_run_mr(onset_date, tmp_path / "a", [2.0] * 40, thresh="10")))
    ds = _run_mr(onset_date, tmp_path / "b", [2.0] * 40, thresh="9.9")
    assert _onset(ds) == np.timedelta64(1, "D")


def test_mr_vetoed_trigger_then_later_onset(tmp_path, onset_date):
    # Trigger at day 0 (5 x 3mm), then 12 dry days: a 10-day window totaling
    # 0mm < 5mm inside the follow-up vetoes it. The search continues to the
    # next trigger candidate (day 17) rather than stopping.
    values = [3.0] * 5 + [0.0] * 12 + [3.0] * 5 + [2.0] * 30
    assert _onset(_run_mr(onset_date, tmp_path, values)) == np.timedelta64(18, "D")


def test_mr_consecutive_dry_veto(tmp_path, onset_date):
    # Trigger at day 0, then 6 dry days (< 7: no veto under consecutive_dry),
    # so day 0 stands; with --mr-dry-spell-days 6 it is vetoed and day 11 wins.
    values = [3.0] * 5 + [0.0] * 6 + [3.0] * 5 + [2.0] * 30
    ds = _run_mr(onset_date, tmp_path / "a", values, "--mr-veto", "consecutive_dry")
    assert _onset(ds) == np.timedelta64(1, "D")
    ds = _run_mr(
        onset_date,
        tmp_path / "b",
        values,
        "--mr-veto",
        "consecutive_dry",
        "--mr-dry-spell-days",
        "6",
    )
    assert _onset(ds) == np.timedelta64(12, "D")


def test_mr_no_onset(tmp_path, onset_date):
    # Never a wet day (>= 1mm), however much it rains in total.
    assert np.isnat(_onset(_run_mr(onset_date, tmp_path, [0.9] * 60, thresh="1")))


def test_mr_nan_disqualifies_candidate(tmp_path, onset_date):
    # The clean-onset series with one missing day inside the follow-up of
    # every otherwise-valid candidate -> NaT (this skill's NaN convention;
    # the reference would ignore the missing follow-up day and report day 10).
    values = [0.0] * 10 + [3.0] * 5 + [2.0] * 30
    values[20] = np.nan
    assert np.isnat(_onset(_run_mr(onset_date, tmp_path, values)))


def test_mr_short_followup(tmp_path, onset_date):
    # Trigger at day 0 of a 20-day series: the 30-day follow-up is cut short.
    # By default the veto is checked over the days available (onset day 0);
    # --mr-reject-short-followup rejects it (NaT).
    values = [3.0] * 5 + [2.0] * 15
    assert _onset(_run_mr(onset_date, tmp_path / "a", values)) == np.timedelta64(1, "D")
    ds = _run_mr(onset_date, tmp_path / "b", values, "--mr-reject-short-followup")
    assert np.isnat(_onset(ds))


def test_mr_search_start(tmp_path, onset_date):
    # A valid onset on May 1 and another on June 12; --mr-search-start 06-02
    # skips the May one. The time dim is absolute, so the output is a date.
    values = [3.0] * 5 + [2.0] * 25 + [0.0] * 12 + [3.0] * 5 + [2.0] * 40
    ds = _run_mr(onset_date, tmp_path / "a", _daily_ds(values))
    assert _onset(ds) == np.datetime64("2026-05-01", "ns")
    ds = _run_mr(onset_date, tmp_path / "b", _daily_ds(values), "--mr-search-start", "06-02")
    assert _onset(ds) == np.datetime64("2026-06-12", "ns")
    assert "from 06-02" in ds["onset_tp_date"].attrs["long_name"]


def test_mr_search_start_needs_absolute_time(tmp_path, onset_date, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_mr(onset_date, tmp_path, [2.0] * 40, "--mr-search-start", "06-02")
    assert exc.value.code == 2
    assert "step-to-time" in capsys.readouterr().err


def test_mr_threshold_field_per_cell_and_member(tmp_path, onset_date):
    # Two cells share a series; their thresholds differ, so their onsets do.
    # Member 1 is dry everywhere. The field has no `number` dim, so it
    # broadcasts across members.
    series = np.array([3.0] * 5 + [5.0] * 5 + [2.0] * 40)
    n = len(series)
    data = np.zeros((2, n, 2, 1))
    data[0] = series[:, None, None]
    ds = xr.Dataset(
        {"tp": (["number", "step", "latitude", "longitude"], data)},
        coords={
            "time": np.datetime64("2026-01-01", "ns"),
            "number": [0, 1],
            "step": np.array([np.timedelta64(d, "D") for d in range(1, n + 1)]),
            "latitude": [1.0, 2.0],
            "longitude": [10.0],
        },
    )
    ds["tp"].attrs.update(units="mm", long_name="Total precipitation")
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["step"].attrs.update(standard_name="forecast_period")
    ds["time"].attrs.update(standard_name="forecast_reference_time", axis="T")

    field = xr.Dataset(
        {"wet_spell_mm": (["latitude", "longitude"], np.array([[10.0], [18.0]]))},
        coords={"latitude": [1.0, 2.0], "longitude": [10.0]},
    )
    field["wet_spell_mm"].attrs["units"] = "mm"
    thr = write_zarr(field, tmp_path / "thr.zarr")

    out = _run_mr(onset_date, tmp_path, ds, "--mr-thresh-field", str(thr), thresh=None)
    onset = out["onset_tp_date"]
    assert onset.dims == ("number", "latitude", "longitude")
    # cell 1: 15mm > 10mm at day 0. cell 2: windows total 15, 17, 19 -> day 2.
    assert onset.sel(number=0, latitude=1.0).values[0] == np.timedelta64(1, "D")
    assert onset.sel(number=0, latitude=2.0).values[0] == np.timedelta64(3, "D")
    assert np.isnat(onset.sel(number=1).values).all()
    assert "per-cell 'wet_spell_mm'" in onset.attrs["long_name"]


def test_mr_requires_exactly_one_threshold(tmp_path, onset_date, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_mr(onset_date, tmp_path / "a", [2.0] * 40, thresh=None)
    assert exc.value.code == 2
    assert "does not invent one" in capsys.readouterr().err

    field = xr.Dataset(
        {"t": (["latitude", "longitude"], np.array([[10.0]]))},
        coords={"latitude": [1.0], "longitude": [10.0]},
    )
    thr = write_zarr(field, tmp_path / "thr.zarr")
    with pytest.raises(SystemExit) as exc:  # both given
        _run_mr(onset_date, tmp_path / "b", [2.0] * 40, "--mr-thresh-field", str(thr))
    assert exc.value.code == 2


def test_mr_threshold_field_grid_mismatch(tmp_path, onset_date, capsys):
    field = xr.Dataset(
        {"t": (["latitude", "longitude"], np.array([[10.0]]))},
        coords={"latitude": [1.5], "longitude": [10.0]},
    )
    thr = write_zarr(field, tmp_path / "thr.zarr")
    with pytest.raises(SystemExit) as exc:
        _run_mr(onset_date, tmp_path / "a", [2.0] * 40, "--mr-thresh-field", str(thr), thresh=None)
    assert exc.value.code == 2
    assert "grid mismatch" in capsys.readouterr().err

    thr2 = write_zarr(xr.Dataset({"t": (["y", "x"], np.array([[10.0]]))}), tmp_path / "thr2.zarr")
    with pytest.raises(SystemExit) as exc:
        _run_mr(onset_date, tmp_path / "b", [2.0] * 40, "--mr-thresh-field", str(thr2), thresh=None)
    assert exc.value.code == 2
    assert "same grid" in capsys.readouterr().err


def test_mr_flags_refused_under_other_definitions(tmp_path, onset_date):
    src = write_zarr(_forecast_ds([2.0] * 30), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            onset_date,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--definition",
            "ICPAC",
            "--mr-thresh",
            "10",
        )
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Oracle: the reference Moron-Robertson implementation.
#
# Ported from python/prepare_data/onset_utils.py (`find_onset`,
# `_precompute_onset`, `_find_onset_core`, `roll_sum_na_rm_left`,
# `roll_sum_na_propagate_left`) of https://github.com/amarchakitus/onset_blending
# at commit 10ec8e3, MIT License, Copyright (c) 2026 University of Chicago.
# Logic kept as in the original; docstrings trimmed and the unused
# params-is-None fallback dropped. Returns a 1-based onset day or None.
# ---------------------------------------------------------------------------

_RefParams = collections.namedtuple(
    "_RefParams",
    [
        "win",
        "wet_day_min_mm",
        "follow_days",
        "mode",
        "min_dry_days",
        "dry_day_min_mm",
        "sum_window",
        "sum_min_mm",
    ],
)


def _ref_roll_sum_na_rm_left(x, k):
    x = np.asarray(x, dtype=float)
    n = len(x)
    if k <= 0 or n < k:
        return np.array([], dtype=float)
    x0 = np.where(np.isnan(x), 0.0, x)
    cs = np.concatenate([[0.0], np.cumsum(x0)])
    return cs[k:] - cs[: n - k + 1]


def _ref_roll_sum_na_propagate_left(x, k):
    x = np.asarray(x, dtype=float)
    n = len(x)
    if k <= 0 or n < k:
        return np.array([], dtype=float)
    na = np.isnan(x).astype(float)
    x0 = np.where(np.isnan(x), 0.0, x)
    cs = np.concatenate([[0.0], np.cumsum(x0)])
    cna = np.concatenate([[0.0], np.cumsum(na)])
    s = cs[k:] - cs[: n - k + 1]
    na_ct = cna[k:] - cna[: n - k + 1]
    s[na_ct > 0] = np.nan
    return s


def _ref_precompute_onset(series, params):
    series = np.asarray(series, dtype=float)
    n = len(series)
    wsum = _ref_roll_sum_na_rm_left(series, params.win)
    if params.mode == "consecutive_dry":
        dry = (~np.isnan(series)) & (series < params.dry_day_min_mm)
        dry_starts = np.zeros(n, dtype=int)
        k = params.min_dry_days
        if n >= k:
            run = np.zeros(n, dtype=int)
            run[0] = int(dry[0])
            for i in range(1, n):
                run[i] = (run[i - 1] + 1) * int(dry[i])
            for e in np.where(run == k)[0]:
                dry_starts[e - k + 1] = 1
        pre_dry = np.concatenate([[0], np.cumsum(dry_starts)])
        return wsum, pre_dry, None
    sw = params.sum_window
    if n >= sw:
        sum_w = _ref_roll_sum_na_propagate_left(series, sw)
        bad = (~np.isnan(sum_w)) & (sum_w < params.sum_min_mm)
        pre_bad = np.concatenate([[0], np.cumsum(bad.astype(int))])
        last_win_start = n - sw
    else:
        pre_bad = np.array([0], dtype=int)
        last_win_start = 0
    return wsum, pre_bad, last_win_start


def _ref_find_onset_core(
    series, n, wsum, aux1, aux2, thresh, params, start_day, reject_if_short_followup
):
    win = params.win
    wmm = params.wet_day_min_mm
    follow = params.follow_days
    max_candidate = len(wsum)
    if max_candidate < 1:
        return None
    min_i = max(1, int(np.ceil(start_day)))
    if min_i > max_candidate:
        return None
    idx = np.arange(min_i, max_candidate + 1)
    wet_mat = np.stack([series[idx - 1 + k] >= wmm for k in range(win)], axis=1)
    all_wet = wet_mat.all(axis=1)
    acc_ok = wsum[idx - 1] > thresh
    base_ok = all_wet & acc_ok
    base_ok = np.where(np.isnan(series[idx - 1]), False, base_ok)
    cand = idx[base_ok]
    if len(cand) == 0:
        return None
    full_end = cand + win - 1 + follow
    if reject_if_short_followup:
        cand = cand[full_end <= n]
        if len(cand) == 0:
            return None
        full_end = full_end[full_end <= n]
    else:
        full_end = np.minimum(n, full_end)
    if params.mode == "consecutive_dry":
        pre_dry = aux1
        lower = cand - 1 + win
        upper = np.minimum(n, cand - 1 + win + follow)
        has_dry_spell = np.where(lower < upper, (pre_dry[upper] - pre_dry[lower]) > 0, False)
    else:
        pre_bad = aux1
        last_win_start = aux2
        sw = params.sum_window
        c_lower = cand - 1 + win
        c_upper = np.minimum(last_win_start + 1, np.maximum(0, full_end - sw + 1))
        max_idx = len(pre_bad) - 1
        c_lower = np.minimum(c_lower, max_idx)
        c_upper = np.minimum(c_upper, max_idx)
        has_dry_spell = np.where(
            c_lower < c_upper, (pre_bad[c_upper] - pre_bad[c_lower]) > 0, False
        )
    ok = ~has_dry_spell
    return int(cand[np.where(ok)[0][0]]) if np.any(ok) else None


def _ref_find_onset(series, thresh, params, reject_if_short_followup=False, start_day=0):
    series = np.asarray(series, dtype=float)
    n = len(series)
    if n < params.win or thresh is None or np.isnan(thresh):
        return None
    wsum, aux1, aux2 = _ref_precompute_onset(series, params)
    return _ref_find_onset_core(
        series, n, wsum, aux1, aux2, thresh, params, start_day, reject_if_short_followup
    )


# --- end of ported reference -------------------------------------------------


def _random_case(rng, mode, n=None):
    """A random rainfall series + definition parameters. Rain arrives in wet
    and dry regimes so triggers, vetoes and later re-triggers all occur; half
    the series are integer-valued so the >=, > and < boundaries are hit
    exactly."""
    n = int(rng.integers(8, 100)) if n is None else n
    p_wet = rng.uniform(0.3, 0.95)
    regime = np.repeat(rng.random(n // 4 + 1) < p_wet, 4)[:n]
    amount = rng.gamma(1.2, 4.0, n)
    if rng.random() < 0.5:
        amount = np.round(amount)
    series = np.where(regime, amount, rng.choice([0.0, 0.5, 1.0], n))
    params = _RefParams(
        win=int(rng.choice([3, 5])),
        wet_day_min_mm=1.0,
        follow_days=int(rng.choice([0, 10, 21, 30])),
        mode=mode,
        min_dry_days=int(rng.choice([3, 5, 7])),
        # sometimes above wet_day_min_mm, so a trigger day can also be "dry"
        dry_day_min_mm=float(rng.choice([1.0, 1.0, 2.0])),
        sum_window=int(rng.choice([5, 10])),
        sum_min_mm=float(rng.choice([5.0, 8.0])),
    )
    thresh = float(rng.uniform(5.0, 30.0))
    if rng.random() < 0.5:
        thresh = float(np.round(thresh))
    if rng.random() < 0.03:
        thresh = np.nan
    start_day = int(rng.integers(0, n // 2 + 1)) if rng.random() < 0.3 else 0
    reject = bool(rng.random() < 0.3)
    return series, params, thresh, start_day, reject


def _kernel(onset_module, block, thresh, params, start_day, reject):
    return onset_module._moron_robertson_onset_nd(
        block,
        thresh,
        window_days=params.win,
        wet_day_thresh=params.wet_day_min_mm,
        follow_days=params.follow_days,
        veto=params.mode,
        dry_spell_days=params.min_dry_days,
        dry_day_thresh=params.dry_day_min_mm,
        sum_window_days=params.sum_window,
        sum_thresh=params.sum_min_mm,
        start_idx=max(0, start_day - 1),  # the reference's start_day is 1-based
        reject_short_followup=reject,
    )


N_ORACLE_SERIES = 400


@pytest.mark.parametrize("mode", ["window_sum", "consecutive_dry"])
def test_mr_matches_reference_on_random_series(onset_module, mode):
    rng = np.random.default_rng(20260927 if mode == "window_sum" else 7291)
    n_onset = n_retriggered = 0
    for _ in range(N_ORACLE_SERIES):
        series, params, thresh, start_day, reject = _random_case(rng, mode)
        ref = _ref_find_onset(series, thresh, params, reject, start_day)
        got = _kernel(onset_module, series[None, :], np.array([thresh]), params, start_day, reject)
        got = None if np.isnan(got[0]) else int(got[0]) + 1
        assert got == ref, (series.tolist(), params, thresh, start_day, reject)
        if ref is not None:
            n_onset += 1
            first_trigger = _ref_find_onset(
                series, thresh, params._replace(follow_days=0), start_day=start_day
            )
            n_retriggered += first_trigger != ref
    # Guard against a vacuous agreement: the sample must exercise found
    # onsets and vetoed-then-later onsets (option A) many times over.
    assert n_onset > N_ORACLE_SERIES // 5
    assert n_retriggered > 10


@pytest.mark.parametrize("mode", ["window_sum", "consecutive_dry"])
def test_mr_batched_kernel_matches_reference(onset_module, mode):
    # One call over a (member, cell, time) block with a per-cell threshold
    # must give every series the same answer as the reference on its own.
    rng = np.random.default_rng(11 if mode == "window_sum" else 12)
    n = 80
    cases = [_random_case(rng, mode, n=n) for _ in range(60)]
    params = cases[0][1]
    block = np.stack([c[0] for c in cases]).reshape(3, 20, n)
    thresh = np.array([c[2] for c in cases[:20]])  # per cell, shared by members
    got = _kernel(onset_module, block, thresh, params, 0, False)
    for m in range(3):
        for c in range(20):
            ref = _ref_find_onset(block[m, c], thresh[c], params)
            want = np.nan if ref is None else ref - 1
            assert np.array_equal(got[m, c], want, equal_nan=True)


# ---------------------------------------------------------------------------
# Onset-definition registry: --definition-ref, provenance, refusal
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_REFERENCES = Path(__file__).resolve().parents[1] / "references" / "onset_definitions.toml"


def _registry():
    with SKILL_REFERENCES.open("rb") as f:
        return tomllib.load(f)["definitions"]


def _hash(entry):
    # The shared contract's recipe, restated here on purpose so the test does
    # not just call the code it is checking.
    sections = ("time_basis", "trigger", "confirm", "veto", "search")
    keep = {k: entry[k] for k in sections if k in entry}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]


def _run(onset_date, tmp_path, ds, *args):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(onset_date, "-i", str(src), "-o", str(out), *args)
    return xr.open_zarr(out, consolidated=True)


def _overrides(ds):
    attr = ds["onset_tp_date"].attrs["onset_definition_overrides"]
    assert ds.attrs["onset_definition_overrides"] == attr
    return json.loads(attr)


ICPAC_WAIVERS = (
    "--waive-field",
    "search.start",
    "--waive-field",
    "search.window_days",
    "--waive-field",
    "veto.follow_anchor",
)


@pytest.mark.parametrize(
    "values",
    [
        [10, 10, 5] + [2.0] * 27,  # onset day 0
        [10, 10, 5] + [0.0] * 8 + [2.0] * 19,  # day 0 vetoed by a dry spell
        [0.0] * 5 + [9, 9, 9] + [2.0] * 30,  # later onset
        [0.0] * 30,  # none
    ],
)
def test_ref_icpac_matches_legacy(tmp_path, onset_date, values):
    ds = _forecast_ds(values)
    legacy = _run(onset_date, tmp_path / "a", ds, "--definition", "ICPAC")
    ref = _run(onset_date, tmp_path / "b", ds, "--definition-ref", "icpac-onset", *ICPAC_WAIVERS)
    assert np.array_equal(_onset(ref), _onset(legacy), equal_nan=True)
    assert ref.attrs["onset_definition_id"] == "icpac-onset"
    # the waived fields are recorded, so the run is not the registered definition
    assert ref.attrs["onset_definition_status"] == "unregistered-variant"
    assert set(_overrides(ref)) == {"search.start", "search.window_days", "veto.follow_anchor"}


def test_ref_icpac_uses_registry_operator(tmp_path, onset_date):
    # A wet spell totaling exactly 20mm: the registry's >= accepts it, the
    # legacy definition's > does not -- and the legacy run records that.
    ds = _forecast_ds([10, 5, 5] + [2.0] * 27)
    ref = _run(onset_date, tmp_path / "a", ds, "--definition-ref", "icpac-onset", *ICPAC_WAIVERS)
    assert _onset(ref) == np.timedelta64(1, "D")
    legacy = _run(onset_date, tmp_path / "b", ds, "--definition", "ICPAC")
    assert _onset(legacy) != np.timedelta64(1, "D")
    assert _overrides(legacy)["trigger.total_op"] == ">"


@pytest.mark.parametrize(
    "values",
    [
        [3.0] * 10 + [1.5] * 20,  # 30mm then 30mm: onset day 0
        [2.0] * 10 + [1.5] * 20,  # 20mm first window: below 25mm
        [0.0] * 5 + [3.0] * 10 + [0.5] * 20,  # confirmation fails
        [0.0] * 4 + [3.0] * 12 + [1.5] * 30,
    ],
)
def test_ref_chc_matches_legacy(tmp_path, onset_date, values):
    ds = _forecast_ds(values)
    legacy = _run(
        onset_date,
        tmp_path / "a",
        ds,
        "--definition",
        "CHC_start_grow_season",
        "--period1-thresh",
        "25",
    )
    ref = _run(onset_date, tmp_path / "b", ds, "--definition-ref", "agrhymet-sos-rolling")
    assert np.array_equal(_onset(ref), _onset(legacy), equal_nan=True)
    assert ref.attrs["onset_definition_status"] == "variant"  # the registry's own status
    assert _overrides(ref) == {}


def test_chc_legacy_default_surfaces_registry_divergence(tmp_path, onset_date):
    ds = _forecast_ds([2.0] * 10 + [1.5] * 20)
    out = _run(onset_date, tmp_path, ds, "--definition", "CHC_start_grow_season")
    reg = _registry()["agrhymet-sos-rolling"]
    assert reg["trigger"]["total_mm"] == 25.0  # AGRHYMET / FEWS NET
    assert out.attrs["onset_definition_id"] == "agrhymet-sos-rolling"
    assert out.attrs["onset_definition_hash"] == _hash(reg)
    assert out.attrs["onset_definition_status"] == "unregistered-variant"
    # PR #115's 20mm default is kept, and shows up as an override of 25mm
    assert _overrides(out) == {"confirm.total_op": ">", "trigger.total_mm": 20.0}


@pytest.mark.parametrize(
    ("ref_id", "legacy_args", "daily"),
    [
        ("moron-robertson-2014", (), False),
        (
            "uchicago-ethiopia-2026",
            ("--mr-veto", "consecutive_dry", "--mr-follow-days", "21"),
            False,
        ),
        ("moron-robertson-india-operational", ("--mr-search-start", "06-02"), True),
    ],
)
def test_ref_mr_matches_legacy(tmp_path, onset_date, ref_id, legacy_args, daily):
    values = [3.0] * 5 + [0.0] * 12 + [3.0] * 5 + [2.0] * 25 + [0.0] * 8 + [3.0] * 5 + [2.0] * 40
    ds = _daily_ds(values) if daily else _forecast_ds(values)
    legacy = _run(
        onset_date,
        tmp_path / "a",
        ds,
        "--definition",
        "Moron_Robertson_2014",
        "--mr-thresh",
        "10",
        *legacy_args,
    )
    ref = _run(onset_date, tmp_path / "b", ds, "--definition-ref", ref_id, "--mr-thresh", "10")
    assert np.array_equal(_onset(ref), _onset(legacy), equal_nan=True)
    assert not np.isnat(_onset(ref))
    # one scalar threshold is not the registry's per-cell climatology
    assert _overrides(ref) == {"trigger.threshold_kind": "scalar", "trigger.total_mm": 10.0}


def _thresh_field(tmp_path):
    field = xr.Dataset(
        {"wet_spell_mm": (["latitude", "longitude"], np.array([[10.0]]))},
        coords={"latitude": [1.0], "longitude": [10.0]},
    )
    field["wet_spell_mm"].attrs["units"] = "mm"
    tmp_path.mkdir(parents=True, exist_ok=True)
    return str(write_zarr(field, tmp_path / "thr.zarr"))


def test_ref_provenance_and_hash(tmp_path, onset_date):
    thr = _thresh_field(tmp_path)
    ds = _run(
        onset_date,
        tmp_path / "a",
        _forecast_ds([0.0] * 10 + [3.0] * 5 + [2.0] * 30),
        "--definition-ref",
        "moron-robertson-2014",
        "--mr-thresh-field",
        thr,
    )
    entry = _registry()["moron-robertson-2014"]
    for attrs in (ds.attrs, ds["onset_tp_date"].attrs):
        assert attrs["onset_definition_id"] == "moron-robertson-2014"
        assert attrs["onset_definition_hash"] == _hash(entry)
        assert attrs["onset_definition_status"] == "canonical"
        assert attrs["onset_definition_overrides"] == "{}"
    assert _onset(ds) == np.timedelta64(11, "D")


def test_ref_explicit_flag_is_override(tmp_path, onset_date):
    thr = _thresh_field(tmp_path)
    ds = _run(
        onset_date,
        tmp_path / "a",
        _forecast_ds([0.0] * 10 + [3.0] * 5 + [2.0] * 30),
        "--definition-ref",
        "moron-robertson-2014",
        "--mr-thresh-field",
        thr,
        "--mr-follow-days",
        "20",
        "--mr-reject-short-followup",
    )
    assert ds.attrs["onset_definition_status"] == "unregistered-variant"
    assert ds.attrs["onset_definition_hash"] == _hash(_registry()["moron-robertson-2014"])
    assert _overrides(ds) == {"kernel.reject_short_followup": True, "veto.follow_days": 20}
    assert "in 20d" in ds["onset_tp_date"].attrs["long_name"]


def _usage_error(onset_date, tmp_path, capsys, *args):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = write_zarr(_forecast_ds([2.0] * 40), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(onset_date, "-i", str(src), "-o", str(tmp_path / "out.zarr"), *args)
    assert exc.value.code == 2
    return capsys.readouterr().err


def test_unknown_ref_lists_known_ids(tmp_path, onset_date, capsys):
    err = _usage_error(onset_date, tmp_path, capsys, "--definition-ref", "icpac")
    for known in ("icpac-onset", "agrhymet-sos-rolling", "moron-robertson-2014"):
        assert known in err


def test_unsupported_registry_field_is_refused(tmp_path, onset_date, capsys):
    err = _usage_error(onset_date, tmp_path / "a", capsys, "--definition-ref", "icpac-onset")
    assert "search.window_days" in err and "--waive-field" in err
    err = _usage_error(onset_date, tmp_path / "b", capsys, "--definition-ref", "agrhymet-sos")
    assert "time_basis" in err and "calendar_dekad" in err
    # a waiver must name a field that was actually refused
    err = _usage_error(
        onset_date,
        tmp_path / "c",
        capsys,
        "--definition-ref",
        "agrhymet-sos-rolling",
        "--waive-field",
        "trigger.total_mm",
    )
    assert "not an unsupported field" in err


def test_ref_and_definition_are_exclusive_and_flags_must_fit(tmp_path, onset_date, capsys):
    err = _usage_error(
        onset_date,
        tmp_path / "a",
        capsys,
        "--definition",
        "ICPAC",
        "--definition-ref",
        "icpac-onset",
    )
    assert "exactly one" in err
    err = _usage_error(
        onset_date,
        tmp_path / "b",
        capsys,
        "--definition-ref",
        "agrhymet-sos-rolling",
        "--wet-spell-days",
        "3",
    )
    assert "--wet-spell-days" in err


def test_references_copy_matches_registry():
    registry = REPO_ROOT / "registry" / "onset_definitions.toml"
    if not registry.is_file():
        pytest.skip("registry/ not present in this checkout")
    # CRLF is normalised only because a Windows checkout with core.autocrlf
    # converts files git has checked out but not one written since; the
    # committed blobs must be byte-identical.
    a = registry.read_bytes().replace(b"\r\n", b"\n")
    b = SKILL_REFERENCES.read_bytes().replace(b"\r\n", b"\n")
    assert a == b, "run python tools/sync_definitions.py"
