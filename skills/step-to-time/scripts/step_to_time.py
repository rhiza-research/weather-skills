# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.3.2",
# ]
# ///
"""Relabel a forecast by valid date: time = init + step, for one init or many inits at one lead."""

from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.6"


@weather_skill(
    name="step-to-time",
    version=_SKILL_VERSION,
)
@weather_skill.argument(
    "-i",
    "--input",
    type=Dataset([("init_time", "prediction_timedelta"), ("time",)]),
    required=True,
)
def step_to_time(ds, **kwargs):
    """Relabel a forecast by valid date: time = init + step, for one init or many inits at one lead."""
    import cftime
    import numpy as np

    if "time" in ds.dims and "step" in ds.coords and ds["step"].ndim == 0:
        # Many inits at one lead: shift each init on the time axis by that lead.
        if ds["time"].attrs.get("standard_name", "forecast_reference_time") != (
            "forecast_reference_time"
        ):
            raise UsageError(
                "time is already on valid dates (its standard_name is not "
                "forecast_reference_time); it holds no inits to shift"
            )
        init = ds["time"].values
        lead = ds["step"].values
        if np.isnat(lead):
            raise UsageError("the forecast lead (scalar step coord) is missing (NaT)")
        if init.dtype == object:
            valid = np.array(
                [t + lead.astype("timedelta64[us]").item() for t in init], dtype=object
            )
        else:
            if np.isnat(init).any():
                raise UsageError("a forecast init on the time axis is missing (NaT)")
            valid = (init + lead).astype("datetime64[ns]")
        if not all(a < b for a, b in zip(valid[:-1], valid[1:], strict=True)):
            raise UsageError(
                "time (inits) must be strictly increasing (no duplicates); sort it first"
            )
        bound_name = ds["step"].attrs.get("bounds")
        if not (isinstance(bound_name, str) and bound_name in ds) and "step_bounds" in ds:
            bound_name = "step_bounds"
        drop = [v for v in ("valid_time", "time_bounds") if v in ds.variables]
        out = ds.drop_vars(drop).assign_coords(time=("time", valid))
        out["time"].attrs = {"standard_name": "time", "axis": "T"}
        if isinstance(bound_name, str) and bound_name in out:
            pairs = np.asarray(out[bound_name].values).astype("timedelta64[us]")
            if init.dtype == object:
                time_pairs = np.array([[t + td.item() for td in pairs] for t in init], dtype=object)
            else:
                time_pairs = (init[:, None] + pairs[None, :]).astype("datetime64[ns]")
            out = out.drop_vars(bound_name).assign_coords(time_bounds=(("time", "nv"), time_pairs))
            out["time"].attrs["bounds"] = "time_bounds"
            out["step"].attrs.pop("bounds", None)
        return out
    if "step" not in ds.dims or "time" in ds.dims:
        raise UsageError(
            "need a single-init forecast (scalar time coord + step dim) or many inits at one "
            "lead (time dim + scalar step coord); for a cube with both time and step dims, "
            "select one step (or one init) first"
        )
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
