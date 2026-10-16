---
name: resolve-time
description: Resolve a relative date query (the last two weeks, today, latest, last 30 days, now-3d) to absolute `--start-time`/`--end-time` or `--date` values from the current UTC date. Use before any fetch when the user said a relative window rather than YYYY-MM-DD. Does not know product lag — for observations (CHIRPS, IMERG, ERA5, stations), run the fetcher's `--probe-latest` first and pass it as `--as-of` so the window is complete. In zsh, pass `--emit iso` values as "${T%/*}" / "${T#*/}" rather than splicing an unquoted $TIME.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py *)
metadata:
  version: "0.0.2"
  catalog-group: agent-tooling
---

# resolve-time

Turn a relative time expression into the absolute `YYYY-MM-DD` values that
fetchers actually accept. The script prints `--start-time`/`--end-time` (or
`--date` for a single day) suitable for splicing into the next skill.

This is **calendar math only**. It does not probe a fetcher or clip to what
is on disk. Fetchers do **not** parse `latest`, `now-3d`, or "last two weeks"
— run this first (or `--probe-latest` when you need the latest published
day), then pass the printed flags through.

## When to use

- The user asked for "the last two weeks", "today", "yesterday", "this month",
  or any other relative / rolling window against the calendar.
- You need today's date as a real calendar value, not a guess.

For the latest day a **product** has published, do not use this skill's
`latest` token. Run that fetcher with `--probe-latest` (stdout is
`YYYY-MM-DD` or `none`) and pass it as `--as-of`. That works for rolling
windows (`last-2w`) and calendar periods (`last-week`, `last-dekad`,
`last-month`) alike: the window then ends on or before the last published
day.

## Observations: probe, then resolve (common path)

Observation products lag real time (CHIRPS 2-7 days, ARCO-ERA5 ~6 days).
Resolving "the last two weeks" against today and fetching cuts the tail:
the fetch exits 0 with fewer days than asked. End the window on the
probed day instead:

```bash
END=$(uv run skills/chirps-fetch/scripts/fetch.py --probe-latest)
T=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-2w --as-of "$END" --emit iso)
uv run skills/chirps-fetch/scripts/fetch.py --start-time "${T%/*}" --end-time "${T#*/}" -o chirps.zarr
```

`--emit iso` plus `"${T%/*}"` / `"${T#*/}"` works in bash and zsh. Splicing
the default `flags` output as an unquoted `$TIME` word-splits in bash but
**not** in zsh (the macOS default shell), where the fetcher then reports
missing `--start-time`.

## Division of labor

This script does **not** parse free-text English. Mapping a phrase to a query
token is the agent's job — the model is good at that. The script does the
deterministic calendar math and the UTC clock.

| Phrase | Token |
|---|---|
| the last two weeks / last 14 days | `last-2w` (same window as `last-14d`) |
| last 7 days / the past week (rolling) | `last-7d` |
| the last month / last 30 days / the past month (rolling) | `last-30d` |
| today / now / latest calendar day | `latest` |
| yesterday | `yesterday` |
| 3 days ago | `now-3d` |
| this week (ISO, Mon–Sun, so far) | `this-week` |
| last week (previous complete ISO week) | `last-week` |
| this dekad so far / last complete dekad (days 1–10, 11–20, 21–end) | `this-dekad` / `last-dekad` |
| this month so far | `this-month` |
| previous calendar month | `last-month` |
| this year so far / previous calendar year | `this-year` / `last-year` |
| March 2026 / calendar 2024 | `2026-03` / `2024` |
| an already-absolute day or span | `2026-08-01` or `2026-08-01/2026-08-14` |

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py <QUERY> [--as-of YYYY-MM-DD] \
    [--emit flags|iso|json]
```

```bash
# Last two weeks ending on the latest published CHIRPS day (observations):
END=$(uv run skills/chirps-fetch/scripts/fetch.py --probe-latest)
T=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-2w --as-of "$END" --emit iso)

# "the last two weeks" on the calendar (UTC today) -- forecasts / non-lagged use:
T=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-2w --emit iso)

# Latest GFS init — probe the fetcher, do not use resolve-time latest:
INIT=$(uv run skills/dynamical-fetch/scripts/fetch.py --probe-latest noaa-gfs-forecast)
```

### Arguments

- `query` (positional) — token from the table above (any case). Not English.
- `--as-of` — clock date `YYYY-MM-DD`. Default: today's date in UTC. Pass it
  to reproduce a window, honor an explicit "as of Friday", or end any window
  (rolling or `last-week` / `last-dekad` / `last-month`) on or before a
  probed latest day. `--as-of none` (a probe that found nothing published)
  exits 2 saying so.
- `--emit` — `flags` (default; splice onto the next skill), `iso`
  (`YYYY-MM-DD` or `START/END`), or `json`.

### Output

- stdout (`flags`): `--start-time YYYY-MM-DD --end-time YYYY-MM-DD` for a
  range, or `--date YYYY-MM-DD` for a single day (`latest`, `yesterday`,
  `now-3d`, `YYYY-MM-DD`). Capture this and splice it into the fetcher.
  Range fetchers that need a one-day window take `--start-time`/`--end-time`
  both set to the probed date — not this skill's `--date` form.
- stderr: `as_of=YYYY-MM-DD`.
- Inclusive dates: `last-1d` is one day; `last-2w` is 14 days ending on
  `as_of`.

Unknown tokens and English phrases exit 2 with an explanation.

## Calendar rules

- Clock is **UTC**. `--as-of` overrides it.
- Weeks are **ISO** (Monday start). `this-week` is Monday through `as_of`.
  `last-week` is the previous complete ISO week.
- `last-<N>d|w|m|y` is a rolling inclusive window ending on `as_of`.
  Months/years clamp the day (31 Jan − 1 month → 28/29 Feb).
  "The last month of data" is `last-30d`, not `last-month`.
- `last-week` / `last-dekad` / `last-month` / `last-year` are the previous
  complete calendar period (ISO week, dekad, named month, calendar year)
  before the one containing `as_of`. When `as_of` is itself the last day of
  a period, that period is not returned — pass the next day as `--as-of` if
  you want it.
- Dekads are calendar dekads: days 1–10, 11–20, and 21 to month end (8–11
  days). `this-dekad` is the dekad start through `as_of`.
- `this-month` / `this-year` are "so far". `YYYY-MM` / `YYYY` are the full
  calendar period (not clipped to today).
- `now-<N>d|w|m|y` is a single date offset from `as_of`.

## Examples

```bash
# Last complete dekad of CHIRPS (bash and zsh):
END=$(uv run skills/chirps-fetch/scripts/fetch.py --probe-latest)
T=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-dekad --as-of "$END" --emit iso)
uv run skills/chirps-fetch/scripts/fetch.py --start-time "${T%/*}" --end-time "${T#*/}" -o chirps.zarr

# Pin the clock (tests / "as of 1 August"):
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-7d --as-of 2026-08-01 --emit iso
```
