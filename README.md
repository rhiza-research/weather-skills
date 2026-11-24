# Rhiza Weather Skills

> ⚠️ **Under active development — not production ready.**
>
> These skills are an early experiment in tool composition for weather/climate
> data pipelines. Interfaces, dataset schema, and skill boundaries may change
> without notice. Fetchers hit real APIs and require credentials; middle-
> pipeline skills have only been smoke-tested on small synthetic data. Do not
> use in any automated workflow you rely on, and do not assume outputs are
> scientifically validated. Expect breakage.

Rhiza Research's weather-skills agents and Claude Code plugin, plus the skills
Rhiza hosts. The plugin bundles skills from four repos:

| Repo | Skills |
|---|---|
| [weather-skills/weather-skills](https://github.com/weather-skills/weather-skills) | Transforms (clip, aggregate, convert, verify, …) and general tools (`resolve-region`, `resolve-time`, `inspect-zarr`, `provenance`) |
| [rhiza-research/weather-skills-fetchers](https://github.com/rhiza-research/weather-skills-fetchers) | Data fetchers (`dynamical-fetch`, `ecmwf-fetch`, `chirps-fetch`, `tahmo-fetch`, …) |
| [weather-skills/weather-skills-plotting](https://github.com/weather-skills/weather-skills-plotting) | `plot`: maps, time series, ensembles and animations from a Jsonnet spec that evaluates to a Plotly figure |
| This repo | The skills below, the `forecaster` and `plotting` agents, and the Claude Code plugin |

Every skill reads and writes one shared Zarr format — see
[`STANDARD_DATASET.md`](https://github.com/weather-skills/weather-skills-core/blob/main/docs/weather-skill-authoring/references/STANDARD_DATASET.md).
The [weather-skills catalog](https://github.com/weather-skills/weather-skills-catalog)
lists skills from every provider.

Built as [Agent Skills](https://agentskills.io) by Rhiza Research.

## Skills

| Skill | What it does |
|---|---|
| `clim-fetch` | Climatology (mean + std) for a `--dataset` (`imerg_final`, `era5`, `chirps`, `ecmwf_ifs`, `oisst`, …), at a selected `--prediction-timedelta` lead and `--window` (days; correctly rolled up, centered, with circular padding if not pre-mirrored), expanded to a `--start-time`/`--end-time` window → Zarr. OISST SST is `--dataset oisst --variable sst`. |
| `cumulus-fetch` | Cumulus AI operational ensemble precip (Aurora S2S run) — 30 members, 46 daily leads, 0.25° or 1° global (`--dataset precip`, `precip_1deg`, `precip_era5`), one init or a range of inits → Zarr. Requires credentials. |
| `cumulus-downscaled-fetch` | Cumulus AI downscaled Aurora precip — 0.1° over East and West Africa, 46 daily leads, diffusion / flow-matching / UNet, ensemble mean or 10 members → Zarr. Requires credentials. |
| `hindcast-fetch` | ECMWF (inits 1996–2025), GEFS (1998–2019) or Cumulus (1996 to 2026-08-31) subseasonal hindcasts for one or more ranges of init dates → multi-init forecast Zarr of daily precip, 2 m temperature and (ECMWF) SST. ECMWF and GEFS need no credentials; Cumulus does. |
| `submit-feedback` | Build a length-checked prefilled GitHub new-issue URL the user clicks to file feedback under their own account. Holds no token, makes no network call, creates no issue itself. |

## Install

### As a Claude Code plugin

Install the plugin once — add the marketplace, then install the plugin:

```bash
claude plugin marketplace add rhiza-research/rhiza-weather-skills
claude plugin install weather-skills@rhiza-research
```

Then run the bundled `forecaster` agent (or `plotting` for figures). The `--allowedTools` flag pre-approves
the plugin's skills for the session so a multi-step pipeline runs end to end
without a prompt at each step:

```bash
claude --agent weather-skills:forecaster --allowedTools "Skill(weather-skills:*)"
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
      "Skill(weather-skills:*)"
    ]
  }
}
```

With that rule in place, plain `claude --agent weather-skills:forecaster`
works without the flag. This lets the plugin's skills run unprompted —
including reaching the network and writing output files — so add an `ask` or
`deny` rule instead if you want to be prompted for some or all of them.

### As a CLI tool

For ad-hoc command-line use (no agent involved), install the skills as a
single `rhiza-weather-skills` binary:

```bash
# One-shot, no install — list available skills
uvx --from git+https://github.com/rhiza-research/rhiza-weather-skills rhiza-weather-skills

# Run one
uvx --from git+https://github.com/rhiza-research/rhiza-weather-skills rhiza-weather-skills <skill> [args]
```

Or install once and invoke directly:

```bash
uv tool install git+https://github.com/rhiza-research/rhiza-weather-skills
rhiza-weather-skills                     # list
rhiza-weather-skills <skill> [args]      # run one
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
npx skillkit install rhiza-research/rhiza-weather-skills --list

# Install all skills to the current project
npx skillkit install rhiza-research/rhiza-weather-skills --all --yes

# Install globally so any project can use them
npx skillkit install rhiza-research/rhiza-weather-skills --all --yes --global

# Target a specific agent (otherwise skillkit installs for every agent it detects)
npx skillkit install rhiza-research/rhiza-weather-skills --all --yes --agent claude-code

# Install just a subset
npx skillkit install rhiza-research/rhiza-weather-skills --skill=clim-fetch
npx skillkit install rhiza-research/rhiza-weather-skills --skills=clim-fetch,hindcast-fetch

# Overwrite an existing install
npx skillkit install rhiza-research/rhiza-weather-skills --all --yes --force
```

Pin in a manifest for team / reproducible use:

```bash
npx skillkit manifest init
npx skillkit manifest add rhiza-research/rhiza-weather-skills
npx skillkit manifest install
```

See `npx skillkit install --help` for more flags.

## Credentials

Fetchers read credentials from environment variables (or `.netrc` where
supported by the underlying client). Nothing is hardcoded. See each skill's
`compatibility:` frontmatter for the specific vars required.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the publishing model (`main` is the
consumer-facing branch — every merge is a release), the PR workflow, and the
version-bump conventions (per-skill `metadata.version` in SKILL.md, driven by `release: major`
/ `release: minor` PR labels, with patch as the default).

## License

MIT. See [`LICENSE`](LICENSE).
