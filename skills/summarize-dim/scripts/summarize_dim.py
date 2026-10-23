# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/weather-skills/weather-skills-core@reduce-module",
# ]
# ///
"""Collapse named dimensions of a Zarr, or each group of a label coord, with a statistic."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.reduce import METHODS, summarize
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import TIME_LABELS, latitude_weights

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.4"


@weather_skill(
    name="summarize-dim",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--dim",
    action="append",
    help="Dim to summarize (repeatable).",
)
@weather_skill.argument(
    "--by",
    action="append",
    help="Label coord to group by; its dims collapse to one value per label (repeatable).",
)
@weather_skill.argument(
    "--method",
    required=True,
    choices=list(METHODS),
)
@weather_skill.argument(
    "--lat-weighted",
    action="store_true",
    help="cos(lat) weights for --method mean over latitude, alone or per --by group.",
)
@weather_skill.argument(
    "--min-coverage",
    type=float,
    help=(
        "With a --by time label: blank values whose group has data for less than this share "
        "(0-1) of its full calendar period. Default 1.0."
    ),
)
def summarize_dim(ds, variable, dim, by, method, lat_weighted, min_coverage, **kwargs):
    """Collapse named dimensions of a Zarr, or each group of a label coord, with a statistic."""
    if not dim and not by:
        raise UsageError("pass --dim, --by, or both")
    time_by = [b for b in by or [] if b in ds.coords and "time" in ds[b].dims]
    if min_coverage is not None and not time_by:
        raise UsageError(f"--min-coverage needs a --by label along time ({', '.join(TIME_LABELS)})")
    weights = None
    if lat_weighted:
        if method != "mean":
            raise UsageError("--lat-weighted requires --method mean")
        lat_dim, _ = detect_spatial_dims(ds)
        collapsed = [*(dim or []), *(d for b in by or [] if b in ds.coords for d in ds[b].dims)]
        if lat_dim not in collapsed:
            raise UsageError(f"--lat-weighted needs --dim {lat_dim} or a --by label over it")
        weights = latitude_weights(ds[lat_dim])
    return summarize(
        ds,
        dims=dim,
        by=by,
        method=method,
        variables=variable,
        weights=weights,
        min_coverage=1.0 if min_coverage is None else min_coverage,
    )


if __name__ == "__main__":
    summarize_dim()
