# Weather Skills

> ⚠️ **Under active development — not production ready.**
>
> These skills are an early experiment in tool composition for weather/climate
> data pipelines. Interfaces, dataset schema, and skill boundaries may change
> without notice. Fetchers hit real APIs and require credentials; middle-
> pipeline skills have only been smoke-tested on small synthetic data. Do not
> use in any automated workflow you rely on, and do not assume outputs are
> scientifically validated. Expect breakage.

Small tools an AI agent can run to fetch weather data and transform it. Each
skill is a command-line script. They share one Zarr file format so outputs of
one skill can feed into the next — see
[`STANDARD_DATASET.md`](https://github.com/weather-skills/weather-skills-core/blob/main/docs/weather-skill-authoring/references/STANDARD_DATASET.md).

Built as [Agent Skills](https://agentskills.io) by Rhiza Research.

## Skills

### Fetchers (ingress — source-specific)

Prefer `dynamical-fetch` when the dynamical.org catalog has the dataset
(GFS, GEFS, ECMWF IFS-ENS, AIFS, ICON-EU, analyses, IMERG, CHIRPS). Use a
credentialed or source-specific fetcher only when it does not.

| Skill | What it does |
|---|---|
| `dynamical-fetch` | **Preferred when the catalog has it.** dynamical.org open catalog (GFS, GEFS, ECMWF IFS-ENS 15-day and 46-day, AIFS, ICON-EU, MRMS, analyses, **IMERG**, **CHIRPS**) via `--dataset`, credential-free → Zarr. Default IMERG source: `nasa-imerg-analysis-late` / `nasa-imerg-analysis-early`. CHIRPS products: `ucsb-chc-chirps-analysis-final` / `ucsb-chc-chirps-analysis-preliminary`. |
| `ecmwf-fetch` | ECDS **fallback** for ECMWF S2S / ER (pre-2026, ocean, unmapped fields). Prefer `dynamical-fetch --dataset ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree` for 2026+ catalog fields. |
| `chirps-fetch` | Default CHIRPS source: dynamical.org final + prelim fallback → `precip` Zarr |
| `imerg-fetch` | Earthdata **daily** IMERG Late/Final fallback (`GPM_3IMERGDL` / `GPM_3IMERGDF`) → Zarr. Default IMERG is `dynamical-fetch`, not this skill. |
| `clim-fetch` | Climatology (mean + std) for a `--dataset` (`imerg_final`, `era5`, `chirps`, `ecmwf_ifs`, `oisst`, …), at a selected `--prediction-timedelta` lead and `--window` (days; correctly rolled up, centered, with circular padding if not pre-mirrored), expanded to a `--start-time`/`--end-time` window → Zarr. OISST SST is `--dataset oisst --variable sst`. |
| `tahmo-fetch` | TAHMO station observations (daily-aggregated) → Zarr |
| `cumulus-fetch` | Cumulus AI operational ensemble precip (Aurora S2S run) — 30 members, 46 daily leads, 0.25° or 1° global (`--dataset precip`, `precip_1deg`, `precip_era5`) → Zarr. Requires credentials. |
| `neuralgcm-fetch` | NeuralGCM S2S ensemble (IMERG precip `tp`, ERA5 surface `t2m`/`d2m`) — up to 50 members, 6-hour leads to 60 days, ~2.8° → Zarr. Requires credentials. |
| `weathernext-fetch` | Google WeatherNext 2 ensemble (2025 on) — 64 members, 6-hour leads to 15 days, 0.25° global, 13 pressure levels → Zarr. Pass `-v` and `--member` (full cube ~1 TB). Requires credentials. |
| `pbc-fetch` | PBC / StillLearning AI Weather Quest precip **quintile probabilities** (`era5-p_pr_19` week 3, `era5-p_pr_26` week 4), 1.5° global → Zarr. Requires credentials. |
| `ecmwf-hres-fetch` | Deterministic ECMWF HRES forecast — 0.25°, out to 15 days, surface and pressure-level fields → Zarr. No credentials; needs the eccodes system library. |
| `arco-era5-fetch` | ERA5 hourly reanalysis (ARCO-ERA5) → Zarr. No credentials. |
| `cmip6-fetch` | One CMIP6 model run (historical 1850–2014 or `ssp*` to 2100) → Zarr. No credentials. |
| `rfe-fetch` | NOAA CPC African Rainfall Estimate (RFE2) — daily, 0.1° over Africa, from 2001 → Zarr. No credentials. |
| `gsmap-fetch` | JAXA GSMaP v6 gauge-corrected hourly 0.1° precip (from April 2025, 60°N–60°S) → Zarr. No credentials. |
| `oisst-fetch` | NOAA OISST v2.1 daily 0.25° sea-surface temperature (1981-09 on) → Zarr. No credentials. |
| `smap-fetch` | NASA SMAP Enhanced L3 (`SPL3SMP_E`) daily 9 km surface soil moisture → Zarr. Requires credentials. |
| `ghcn-daily-fetch` | NOAA GHCN-Daily station observations (daily precip and max/min/mean temperature, ~130,000 stations) → station Zarr. No credentials. |
| `openaq-fetch` | OpenAQ air-quality station observations (daily mean PM2.5, PM10, NO2, O3, SO2, CO) → station Zarr. Requires credentials. |

### Generic middle (operate on any standard dataset)
| Skill | What it does |
|---|---|
| `resolve-region` | Resolve an ISO 3166-1 alpha-3 country code or sub-national region to a `--bbox N/W/S/E` (and optional boundary polygon GeoJSON) |
| `resolve-time` | Resolve relative calendar dates ("the last two weeks", `latest`, `now-3d`) to `--start-time`/`--end-time` or `--date`. Latest published day is the fetcher's `--probe-latest`, not this skill. |
| `inspect-zarr` | Print dimension sizes, coordinate values, and a bounded data-variable summary (min/max/mean, finite/NaN, truncated sample) of a Zarr (stdout only; never dumps full arrays) |
| `clip-region` | Subset a gridded Zarr to a `--bbox N/W/S/E` (use `resolve-region` for a country's bbox) |
| `aggregate-temporal` | Resample rates along `time`/`step` (mean/min/max); duration-weights CF bounds; stamps `data_interval` = the output spacing (the `--period`), plus `aggregation_period` + `aggregation_coverage` + `cell_methods` |
| `convert-to-totals` | Terminal: rate × stamped `aggregation_period` → amount (100% coverage default; refuses overlapping Δt < period — `select` first) |
| `deaccumulate` | Convert a leftover cumulative-since-init forecast variable into per-step diffs along the `step` axis (fetchers already write rates) |
| `step-to-time` | Realize a forecast's `step` lead-time axis as wall-clock valid times (`time = init + step`) so it can be compared against time-based observations |
| `select` | Pick entries along one dim by position or coordinate value; a single pick collapses the dim (e.g. one forecast week from each model before `concat`) |
| `convert-calendar` | Convert the time axis to another CF calendar (`standard`, `noleap`, `360_day`, …) so two datasets share one calendar before comparing |
| `unit-convert` | Convert a variable to target `--to-units` (e.g. precip flux `kg m-2 s-1` → depth rate `mm/day` via ÷ liquid-water density) |
| `downscale` | Spatial downscaling onto a finer grid (by factor, finer resolution, or a reference grid) via `--algorithm` (`linear-interpolation` or `q-q` empirical quantile mapping) |
| `coarsen` | Coarsen or align a grid by linear interpolation onto a target `(resolution, offset)` — geometry only, adds no information |
| `edit-coords` | Rename data variables, coordinates, or dims (`--rename OLD=NEW`) and re-index a dim by another 1-D coordinate (`--swap-dims OLD=NEW`); refuses edits that lose an ontology dim |
| `drop` | Drop named data variables or non-index coordinates (`--name`) — e.g. stray `valid_time` / `spatial_ref` before `concat` |
| `concat` | Join Zarr stores along a named dim (incl. new dims with coord values) |
| `merge` | Combine the variables of several Zarrs into one dataset on their shared coordinates |
| `summarize-dim` | Summarize named dims with a statistic (mean/std/min/max/sum/median) — e.g. ensemble spread as the std across `number`, or a time-mean baseline |
| `point-value` | Sample a gridded dataset at point locations (station Zarr, `--point LAT,LON[,ID]`, or CSV) into a `station_id` point_obs dataset — nearest, bilinear, or N×N cell-mean — for grid-vs-station `difference` / `verify` |
| `local-extrema` | Find the local highs or lows of a gridded field (pressure centres, rainfall maxima) into a `station_id` point_obs dataset of locations and central values — for H / L map labels or listing centres |
| `difference` | Subtract one dataset from another (A − B) with inner-join alignment and broadcasting — anomalies vs a baseline, scenario-minus-historical change maps |
| `standardize-anomaly` | Standardized anomaly aka z-score: `(field − clim_avg) / clim_std` against a climatology (e.g. `clim-fetch`) — dimensionless output, errors on units mismatch. For a plain physical-unit anomaly, use `difference` instead. |
| `zonal-moisture-transport` | Eastward moisture flux `q·u`, default column-integrated to IVT (`viwve`, kg m-1 s-1). Compose after `dynamical-fetch` (46-day `pressure_level` group) with `q` and `u` |
| `verify` | Forecast vs obs verification: `--metric hits|bias|mae` (hits = event classification). |
| `indicator` | Daily or weekly boolean indicator from one `--rule` (aliases `icpac-onset` / `chc-onset`, or clauses like `precip sum 8d >= 25`); optional `--probability`, `--detect first` / `any`, `--cumulative` |

### Agent capabilities
Capabilities the agent uses alongside pipelines; none of them produces a
dataset output.

| Skill | What it does |
|---|---|
| `resolve-time` | Resolve relative calendar dates to absolute `--start-time`/`--end-time` or `--date`. |
| `inspect-zarr` | Print dims, coordinate values, and a bounded data-variable summary of a Zarr (stdout; no write). Data arrays can be huge — this skill never dumps them in full. |
| `provenance` | Inspect `weather_skills_history` on a Zarr or a PNG (DAG lineage, JSON, or a commit-pinned reproduction script). |
| `submit-feedback` | Build a length-checked prefilled GitHub new-issue URL the user clicks to file feedback under their own account. Holds no token, makes no network call, creates no issue itself. |

## Install

These skills live at <https://github.com/rhiza-research/forecasting-skills>.
There are three ways to use them.

### As a Claude Code plugin

Install the plugin once — add the marketplace, then install the plugin:

```bash
claude plugin marketplace add rhiza-research/forecasting-skills
claude plugin install rhiza-forecasting@weather-skills
```

Then run the bundled `forecaster` agent. The `--allowedTools` flag pre-approves
the plugin's skills for the session so a multi-step pipeline runs end to end
without a prompt at each step:

```bash
claude --agent rhiza-forecasting:forecaster --allowedTools "Skill(rhiza-forecasting:*)"
```

Fetchers still need their credentials in the environment; see each skill's
`compatibility:` frontmatter for what it reads (for example an
`EXAMPLE_API_KEY`).

To make the approval permanent instead of passing the flag every time, add the
same rule to `permissions.allow` in a settings file (`.claude/settings.json` for
one project, `~/.claude/settings.json` for every project):

```json
{
  "permissions": {
    "allow": [
      "Skill(rhiza-forecasting:*)"
    ]
  }
}
```

With that rule in place, plain `claude --agent rhiza-forecasting:forecaster`
works without the flag. This lets the plugin's skills run unprompted —
including reaching the network and writing output files — so add an `ask` or
`deny` rule instead if you want to be prompted for some or all of them.

### As a CLI tool

For ad-hoc command-line use (no agent involved), install the skills as a
single `forecasting-skills` binary:

```bash
# One-shot, no install — list available skills
uvx --from git+https://github.com/rhiza-research/forecasting-skills forecasting-skills

# Run one
uvx --from git+https://github.com/rhiza-research/forecasting-skills forecasting-skills <skill> [args]
```

Or install once and invoke directly:

```bash
uv tool install git+https://github.com/rhiza-research/forecasting-skills
forecasting-skills                          # list
forecasting-skills <skill> [args]           # run one
```

Each skill's PEP 723 inline dependency block is resolved by `uv run`
on each invocation, so the runner itself contributes no Python deps to the
script's runtime environment.

### As agent skills

For use by an LLM agent (Claude Code, etc.), install the `SKILL.md` files
into your project with [skillkit](https://github.com/rohitg00/skillkit) — no
local install needed, `npx` runs the latest skillkit on demand (add `@latest`
to always pull the newest):

```bash
# List what skillkit discovers in the repo
npx skillkit install rhiza-research/forecasting-skills --list

# Install all skills to the current project
npx skillkit install rhiza-research/forecasting-skills --all --yes

# Install globally so any project can use them
npx skillkit install rhiza-research/forecasting-skills --all --yes --global

# Target a specific agent (otherwise skillkit installs for every agent it detects)
npx skillkit install rhiza-research/forecasting-skills --all --yes --agent claude-code

# Install just a subset
npx skillkit install rhiza-research/forecasting-skills --skill=ecmwf-fetch
npx skillkit install rhiza-research/forecasting-skills --skills=clip-region,difference

# Overwrite an existing install
npx skillkit install rhiza-research/forecasting-skills --all --yes --force
```

Pin in a manifest for team / reproducible use:

```bash
npx skillkit manifest init
npx skillkit manifest add rhiza-research/forecasting-skills
npx skillkit manifest install
```

See `npx skillkit install --help` for more flags.

## Composition pattern

Middle-pipeline skills are designed to chain. Example — a weekly forecast and
the matching observations for one country, using the `forecasting-skills` CLI from
the Install section above:

```bash
# Kenya bbox (N/W/S/E) from resolve-region
forecasting-skills resolve-region KEN

# ECMWF 46-day ensemble forecast, weekly totals
forecasting-skills dynamical-fetch \
    --dataset ecmwf-ifs-ens-forecast-46-day-daily-1-5-degree \
    --date 2026-09-14 --bbox 5.5/33.9/-4.7/41.9 -v tp \
    --output /tmp/ecmwf.zarr
forecasting-skills aggregate-temporal \
    --input /tmp/ecmwf.zarr --period weekly --output /tmp/ecmwf_weekly.zarr
forecasting-skills convert-to-totals \
    --input /tmp/ecmwf_weekly.zarr --output /tmp/ecmwf_weekly_totals.zarr

# CHIRPS observations for the same weeks
forecasting-skills chirps-fetch \
    --start-time 2026-09-14 --end-time 2026-09-20 --bbox 5.5/33.9/-4.7/41.9 \
    --output /tmp/chirps.zarr
forecasting-skills aggregate-temporal \
    --input /tmp/chirps.zarr --period weekly --output /tmp/chirps_weekly.zarr
forecasting-skills convert-to-totals \
    --input /tmp/chirps_weekly.zarr --output /tmp/chirps_weekly_totals.zarr
```

In practice a user just states the goal in natural language and the agent
picks and composes skills from this set.

## Standard dataset contract

Skills share a simple Zarr contract: fixed dimension names (`latitude`,
`longitude`, `time`, `step`, `number`, `station_id`, …) and short types (`spatial`, `observations`, `forecast`,
`vertical_forecast`, `point_obs`, …).
See
[`STANDARD_DATASET.md`](https://github.com/weather-skills/weather-skills-core/blob/main/docs/weather-skill-authoring/references/STANDARD_DATASET.md).
Fetchers write that shape; other skills only depend on dims, coords, data
variables, and `weather_skills_*` attrs — not on per-variable encoding.

## CLI flag conventions

Each skill declares its CLI through the `@weather_skill` decorator from
`weather_skills_core`, so common parameters (`--input` / `-o`, `--bbox`,
`--start-time` / `--end-time`, etc.) mean the same thing wherever they appear.
See
[`CONVENTIONS.md`](https://github.com/weather-skills/weather-skills-core/blob/main/docs/weather-skill-authoring/references/CONVENTIONS.md)
for the full mapping of concept → canonical flag.

## Credentials

Fetchers read credentials from environment variables (or `.netrc` where
supported by the underlying client). Nothing is hardcoded. See each skill's
`compatibility:` frontmatter for the specific vars required.

## Alternatives considered

See [`ALTERNATIVES.md`](ALTERNATIVES.md) for the tools we surveyed (CDO, GDAL,
xcube, xclim, zarrs_tools, …) and why the current lightweight xarray-based
implementations are the right trade at this scale.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the publishing model (`main` is the
consumer-facing branch — every merge is a release), the PR workflow, and the
version-bump conventions (per-skill `metadata.version` in SKILL.md, driven by `release: major`
/ `release: minor` PR labels, with patch as the default).

## License

MIT. See [`LICENSE`](LICENSE).
