---
name: resolve-time
description: "Turn a relative date token (last-2w, latest, yesterday, last-dekad, this-month, now-3d) into absolute YYYY-MM-DD start/end or single-day values, from the current UTC date or a given as-of date. Use before a fetch when the user asked for a relative window instead of explicit dates."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py *)
metadata:
  version: "0.0.4"
  catalog-group: agent-tooling
---

# resolve-time

Does the calendar math for a relative date token and prints the absolute dates a fetcher accepts.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py QUERY [--as-of YYYY-MM-DD] [--emit flags|iso|json]
```

### Arguments
- `query` (positional) — a token, any case; not free English. Map the phrase yourself:
  - `latest` / `now` / `today`, `yesterday`, `now-<N>d|w|m|y` (one day N units before).
  - `last-<N>d|w|m|y` — rolling inclusive window ending on the as-of day (`last-2w` = 14 days; "the last month" = `last-30d`).
  - `this-week|dekad|month|year` — period start through the as-of day.
  - `last-week|dekad|month|year` — the previous complete period.
  - `YYYY-MM-DD`, `YYYY-MM-DD/YYYY-MM-DD`, `YYYY-MM`, `YYYY` — absolute; months and years are the full period.
- `--as-of` — the day to count from. Default: today's UTC date.
- `--emit` — stdout format. Default: `flags`.
  - `flags`: `--start-time START --end-time END`, or `--date DAY` for a single-day token.
  - `iso`: `START/END`, or `DAY`.
  - `json`: keys `query`, `as_of`, `start_time`, `end_time`, `date` (null for ranges), `time` (`date` or `range`), `flags`.

### Output
One line on stdout; `as_of=YYYY-MM-DD` on stderr. All ranges are inclusive. Unknown tokens, free text, and `--as-of none` exit 2.

### Notes
- Weeks are ISO weeks (Monday to Sunday). Dekads are days 1–10, 11–20, and 21 to month end. Months and years clamp the day (31 Mar minus 1 month is 28/29 Feb).
- `last-week|dekad|month|year` never includes the period containing the as-of day, even when the as-of day is its last day; pass the next day as `--as-of` to get it.
- The token `latest` is today's date, not the newest published data. Observations lag real time (CHIRPS 2–7 days, ERA5 about 6). For them, pass the fetcher's `--probe-latest` result as `--as-of` so the window ends on published data. A probe that printed `none` makes `--as-of none` exit 2.
- A range fetcher's one-day window is `--start-time D --end-time D`, not this skill's `--date D`.
- In zsh, an unquoted `$T` holding the `flags` output is not split into words. Use `--emit iso` and pass `"${T%/*}"` and `"${T#*/}"`, which works in bash and zsh.

### Example
```bash
T=$(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py last-dekad --as-of 2026-10-13 --emit iso)
```
Sets `T=2026-10-01/2026-10-10`, the last complete dekad on or before 13 October 2026.
