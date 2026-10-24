# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
# ]
# ///
"""Relabel a single-init forecast by valid date: its step axis becomes a time axis of init + step."""

from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"


@weather_skill(
    name="step-to-time",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "-i", "--input", type=Dataset("init_time, prediction_timedelta"), required=True
)
def step_to_time(ds, **kwargs):
    """Relabel a single-init forecast by valid date: its step axis becomes a time axis of init + step."""
    import cftime
    import numpy as np

    if "step" not in ds.dims or "time" in ds.dims:
        raise UsageError("need forecast shape: scalar time coord + step dim")
    init = ds["time"]
    step = ds["step"]
    init_scalar = init.values.item()
    if not isinstance(init_scalar, cftime.datetime) and np.isnat(init.values):
        raise UsageError("forecast init (scalar time coord) is missing (NaT)")
    if not (np.diff(step.values.astype("timedelta64[ns]").astype(np.int64)) > 0).all():
        raise UsageError("step must be strictly increasing (no duplicates); sort it first")
    if isinstance(init_scalar, cftime.datetime):
        valid = np.array(
            [init_scalar + td.item() for td in step.values.astype("timedelta64[us]")],
            dtype=object,
        )
        init_iso = init_scalar.isoformat()
    else:
        valid = (init.values + step.values).astype("datetime64[ns]")
        init_iso = str(np.datetime_as_string(init.values.astype("datetime64[s]")))
    drop = ["time"] + (["valid_time"] if "valid_time" in ds.variables else [])
    bound_name = ds["step"].attrs.get("bounds") if "step" in ds.coords else None
    if not (isinstance(bound_name, str) and bound_name in ds) and "step_bounds" in ds:
        bound_name = "step_bounds"
    out = ds.drop_vars(drop).rename({"step": "time"}).assign_coords(time=("time", valid))
    out["time"].attrs.setdefault("standard_name", "time")
    out["time"].attrs.setdefault("axis", "T")
    if isinstance(bound_name, str) and bound_name in out:
        pairs = np.asarray(out[bound_name].values)
        if isinstance(init_scalar, cftime.datetime):
            time_pairs = np.array(
                [
                    [init_scalar + td.item() for td in row]
                    for row in pairs.astype("timedelta64[us]")
                ],
                dtype=object,
            )
        else:
            time_pairs = (init.values + pairs).astype("datetime64[ns]")
        out = out.drop_vars(bound_name).assign_coords(time_bounds=(("time", "nv"), time_pairs))
        out["time"].attrs["bounds"] = "time_bounds"
    out.attrs["weather_skills_forecast_init"] = init_iso
    return out


if __name__ == "__main__":
    step_to_time()
