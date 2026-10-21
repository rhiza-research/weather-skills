"""GEFS daily ensemble mean (init INIT_GEFS) -> weekly means aligned to the ECMWF init.

Usage: python gefs_2026_weekly.py GEFS_DAILY.zarr OUT.zarr ECMWF_INIT(YYYY-MM-DD) LAG_DAYS
Week k covers GEFS leads [LAG + 7k, LAG + 7k + 7) days, i.e. the same valid days as
ECMWF lead week k; the result is labelled with the ECMWF init, as in training.
"""

import sys

import numpy as np
import xarray as xr

src, out, init, lag = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
g = xr.open_zarr(src).load()
da = g[list(g.data_vars)[0]]
lead = (da.step.values / np.timedelta64(1, "D")).astype(int)
steps = np.array([0, 7, 14, 21])
windows = [(lead >= lag + s) & (lead < lag + s + 7) for s in steps]
if any(int(w.sum()) != 7 for w in windows):
    raise SystemExit("GEFS run too short for 4 full weeks after the lag")
w = np.stack([da.isel(step=m).mean("step").values for m in windows])
ds = (
    xr.DataArray(
        w[None],
        dims=("time", "step", "latitude", "longitude"),
        coords={
            "time": [np.datetime64(init, "ns")],
            "step": steps.astype("timedelta64[D]").astype("timedelta64[ns]"),
            "latitude": da.latitude.values,
            "longitude": da.longitude.values,
        },
    )
    .astype("float32")
    .to_dataset(name="precip")
)
ds["precip"].attrs.update(units="mm day-1", aggregation_period="7 day")
ds.to_zarr(out, mode="w", consolidated=True)
print("gefs weekly", dict(ds.sizes))
