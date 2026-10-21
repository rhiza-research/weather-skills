"""Observed weekly events per OND season from CHIRPS, for the blend to predict.

Usage: python events.py WORK
Runs the indicator skill on each $WORK/chirps/c15_YYYY.zarr (1981-2025, 1 Oct - 31 Jan
daily) and concatenates every season into $WORK/obs_event.zarr.

Settings (environment):
  EVENT_RULE      indicator rule (default icpac-onset), e.g. "precip sum 7d >= 25"
  EVENT_FIRST_IN  window for a first-event target (default 7d): 1 if the first event of
                  the season falls in the week from each date, NaN once it has happened.
                  Set it empty for an every-week event such as a wet week: then the rule's
                  own 7-day window is the week and each date is 1 or 0 on its own.
"""

import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parent)
)  # skillrun.py beside this script (python -I drops it)
from skillrun import run  # noqa: E402

W = Path(sys.argv[1])
RULE = os.environ.get("EVENT_RULE", "icpac-onset")
FIRST_IN = os.environ.get("EVENT_FIRST_IN", "7d")
extra = ["--first-in", FIRST_IN] if FIRST_IN else []
for y in range(1981, 2026):
    ev = W / "chirps" / f"ev_{y}.zarr"
    if not ev.exists():
        run(
            "indicator",
            "indicator",
            "-i",
            W / "chirps" / f"c15_{y}.zarr",
            "-v",
            "precipitation_surface",
            "--rule",
            RULE,
            *extra,
            "-o",
            ev,
        )
# Every season, so any hindcast years can be scored (lookups pick out the dates they need).
inputs = [a for y in range(1981, 2026) for a in ("-i", W / "chirps" / f"ev_{y}.zarr")]
run("concat", "concat", *inputs, "--dim", "time", "-o", W / "obs_event.zarr")
print(f"events done: {RULE}" + (f", first in {FIRST_IN}" if FIRST_IN else ", every week"))
