# agentic/ — contracts for agent routing (optional)

This PR is optional. It only matters if you want agents choosing Weather Skills to be auditable,
or you want to train or evaluate a learned router. It changes no skill's behaviour, adds no CLI,
and its only runtime dependency is `weather-skills-core`.

## Hypothesis

An agent choosing skills can be made safer, and its choices recorded as clean training data,
if two things are split:

- **Eligibility** — which actions are valid right now — is decided by rules, and those rules are
  checked against the real skill code.
- **Ranking** among the eligible actions is left to an LLM or a learned model.

A corollary can be tested today: rules written from `SKILL.md` alone will be wrong in places, and
running them against the code shows where.

## Pieces

| File | Role |
|---|---|
| `state.py` | Canonical scientific state, built from what a dataset *means*: core dimension ontology, `classify_variable`, stamped `aggregation_period` / `data_interval` / provenance. It ignores naming, dimension order, chunking, variable order and paths, and never reads array values. |
| `predicates.py` | Eligibility with reason codes in three classes (syntactic, scientific, resource). Each rule is tagged `code` (the skill itself refuses) or `layer` (the skill would run anyway, i.e. a silent-failure guard). |
| `events.py` | Opt-in JSONL routing log (`WEATHER_SKILLS_ROUTING_LOG`). It records the state, the eligible and invalid actions with their reasons, the choice and who made it, and the outcome. It refuses file paths in logged parameters. |
| `actions.json`, `catalog.py` | Minimal per-action metadata (semantic version, nearest confusions) and a content hash stamped on every event. |

## What checking against the code found

`tests/test_predicates_conformance.py` runs the real skill functions on fixtures and requires:

- a `code` reason means the skill refuses;
- `layer` reasons only means the skill runs;
- eligible means the skill runs.

Writing it corrected rules that had been taken from the docs:

- `aggregate-temporal` refuses monthly aggregation on a lead axis;
- `verify` tolerates a 1 % grid-spacing difference;
- `ecmwf-fetch` needs only `ECMWF_DATASTORES_KEY`, because the code defaults the URL although
  `SKILL.md` asks for both;
- `smap-fetch` accepts a `~/.netrc` entry.

It also confirmed a silent failure: `difference` on partly overlapping grids runs and returns only
the overlap.

## Relation to what already exists

- **weather-skills-core linter, rule `WSK301` ("SKILL.md drift").** `WSK301` checks that a
  skill's *declared* arguments match its `SKILL.md`. The conformance tests here extend that idea
  from signatures to **behaviour**: does the skill really refuse what its description, and these
  rules, say it refuses? A behavioural check could become a linter rule later.
- **weather-skills-chat tracing (Langfuse).** The chat already traces conversations, LLM calls
  and tool runs, including the full tool specs the model saw. That is the natural source of
  *real* routing data. The event log here is the typed, skill-side counterpart. It records the
  canonical dataset state, the eligible and invalid actions with reason codes, and the outcome,
  none of which a transcript contains. The two would join on the provenance chain.

## Scope

- Eligibility is **skill-level only**. Parameter-level validity stays with each skill; for
  example, `verify --metric crps` on a deterministic forecast is refused by `verify` itself.
- **Fetchers** are eligible without inputs and gated only on credentials. Which variables and
  regions a source covers is catalogue knowledge, not modelled here.

## Related PRs

1. `verify` probabilistic metrics, the three contradiction tests, and the `indicator` dask fix.
2. The onset-definition registry, with `indicator` wired to it.
3. Moron-Robertson onset and `onset-date` registry wiring, stacked on #115.

This PR and PR 2 both add entries to `testpaths` in `pyproject.toml`; whichever merges second
needs a one-line merge there.

## Baseline

CI on `dev` @ `22c2d35` was already red before this branch:

- `aggregate-temporal::test_aggregate_21_day_partial_stamps_coverage` fails;
- `imerg-clim-fetch` has a test but no script;
- `ruff` reports 13 unformatted files and 5 lint findings in `skills/`.

None of these are touched here.
