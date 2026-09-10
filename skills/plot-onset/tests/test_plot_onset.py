"""Correctness tests for plot-onset."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr
from weather_skills_core.provenance import load_figure_history


@pytest.fixture(scope="module")
def plot_onset():
    return load_skill("plot-onset", "plot_onset").plot_onset


def _onset_ds(dates, members=None, name="onset_tp_date", n_lat=2, n_lon=2):
    """Grid of onset dates. `dates` is a flat list cycled over the grid
    (and over members when given); None means NaT (member found no onset)."""

    def to_dt(values):
        return np.array(
            [np.datetime64("NaT", "ns") if v is None else np.datetime64(v, "ns") for v in values]
        )

    cells = n_lat * n_lon
    coords = {
        "latitude": np.linspace(1.0, 2.0, n_lat),
        "longitude": np.linspace(10.0, 11.0, n_lon),
    }
    if members is None:
        data = to_dt([dates[i % len(dates)] for i in range(cells)]).reshape(n_lat, n_lon)
        dims = ["latitude", "longitude"]
    else:
        flat = [dates[i % len(dates)] for i in range(members * cells)]
        data = to_dt(flat).reshape(members, n_lat, n_lon)
        dims = ["number", "latitude", "longitude"]
        coords["number"] = list(range(members))

    ds = xr.Dataset({name: (dims, data)}, coords=coords)
    ds[name].attrs.update(long_name="onset date", standard_name=None)
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    return ds


def test_ensemble_onset_writes_png(tmp_path, plot_onset):
    src = write_zarr(
        _onset_ds(["2026-11-05", "2026-11-12", None, "2026-11-20"], members=4),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "onset.png"

    run_skill(
        plot_onset,
        "-i",
        str(src),
        "-o",
        str(out),
        "--start-date",
        "2026-11-01",
        "--end-date",
        "2026-12-10",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]["skill"] == "plot-onset"
    assert history[-1]["args"]["start_date"] == "2026-11-01"


def test_deterministic_onset_writes_png(tmp_path, plot_onset):
    src = write_zarr(_onset_ds(["2026-11-05", "2026-11-12"]), tmp_path / "in.zarr")
    out = tmp_path / "onset.png"

    run_skill(plot_onset, "-i", str(src), "-o", str(out))

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_scale_spans_new_year(tmp_path, plot_onset):
    """A Dec init running into January must not wrap the scale (day-of-year
    would send Jan 5 back to 5 and invert the range)."""
    src = write_zarr(
        _onset_ds(["2026-12-20", "2027-01-05", None, "2026-12-28"], members=4),
        tmp_path / "in.zarr",
    )
    out = tmp_path / "onset.png"

    run_skill(
        plot_onset,
        "-i",
        str(src),
        "-o",
        str(out),
        "--start-date",
        "2026-12-15",
        "--end-date",
        "2027-01-20",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_all_nat_errors(tmp_path, plot_onset):
    src = write_zarr(_onset_ds([None], members=3), tmp_path / "in.zarr")
    out = tmp_path / "onset.png"

    with pytest.raises(SystemExit) as exc:
        run_skill(plot_onset, "-i", str(src), "-o", str(out))
    assert exc.value.code == 1
    assert not Path(out).exists()


def test_rejects_lead_time_duration(tmp_path, plot_onset):
    ds = xr.Dataset(
        {
            "onset_tp_date": (
                ["latitude", "longitude"],
                np.array([[1, 2], [3, 4]], "timedelta64[D]"),
            )
        },
        coords={"latitude": [1.0, 2.0], "longitude": [10.0, 11.0]},
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "onset.png"

    with pytest.raises(SystemExit) as exc:
        run_skill(plot_onset, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def test_rejects_reversed_dates(tmp_path, plot_onset):
    src = write_zarr(_onset_ds(["2026-11-05"], members=2), tmp_path / "in.zarr")
    out = tmp_path / "onset.png"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_onset,
            "-i",
            str(src),
            "-o",
            str(out),
            "--start-date",
            "2026-12-10",
            "--end-date",
            "2026-11-01",
        )
    assert exc.value.code == 2
