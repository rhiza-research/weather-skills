# Onset-definition registry (draft)

`onset_definitions.toml` states each rainy-season onset definition once — ICPAC, AGRHYMET
start of season, Moron-Robertson and their variants — with its source, the fields that
source leaves unspecified, and what an optimiser may tune. `onset.py` loads and validates
it, hashes each entry, and compiles an entry to the `indicator` skill's `--rule` grammar
(reporting what the grammar cannot express).

Before this, the definitions were hard-coded in `skills/indicator/spec.py` (`ALIASES`) and
in the `onset-date` skill proposed in PR #115, and the copies already disagreed.

## Status

| status      | meaning |
|-------------|---------|
| `canonical` | An institution's or paper's published rule, cited. Agents may select it, never edit it. |
| `variant`   | A documented re-parameterisation of another entry (`derived_from` + `why`). |
| `candidate` | Output of an optimisation. Carries `derived_from`, `why` and an `[optimization]` table (`objective`, `data`, `method`, `validation`, `date`, `parent_hash`, `distance_from_parent`). May change only fields its parent declares tunable, inside their bounds. Never replaces a canonical entry. |

Every non-candidate entry has a `[definitions.<id>.tunable]` table: dotted field →
`{min, max}` or `{choices}`, a `fixed` list of fields that must never be tuned, and a
one-line `why`. On a canonical entry the bounds describe the neighbourhood a candidate may
explore; they are not permission to edit the entry. AGRHYMET's thresholds, comparisons,
dekad windows and time basis are fixed; ICPAC's maproom offers its thresholds as adjustable
"e.g." values, so they are tunable within about half to double the defaults.

## Known drift: three implementations, one organisation

As of 2026-09-27. Sheerwater is the team's benchmark (`sheerwater/interfaces/events.py`),
`indicator` is on `dev`, and `onset-date` is as proposed in PR #115. None of the three refers
to the others. This table is the reason for the registry.

| Definition | Published source | Sheerwater | `indicator` | `onset-date` (#115) |
|---|---|---|---|---|
| ICPAC wet event | e.g. 20 mm in 3 days (IRI maproom) | 3-day sum **> 21** | 3-day sum **≥ 20** | 3-day sum **> 20** |
| ICPAC second condition | no 7-day dry spell within 21 days | **next 7-day sum > 10.5** | no 7-day run < 1 mm within 21 days | same as `indicator` |
| CHC first window | at least 25 mm in a dekad | 10-day sum **> 25** | 10-day sum **> 25** | 10-day sum **≥ 20** |
| CHC confirmation | at least 20 mm over the next two dekads | next 20-day **> 20** | next 20-day **> 20** | 20-day **> 20** |
| CHC time basis | calendar dekads | rolling | rolling | rolling |
| Moron-Robertson | per-cell 5-day climatology, all days wet, no 10-day window < 5 mm within 30 days | **fixed 38 mm**, next 10-day > 5 mm | — | — |

The registry records every column as its own entry (`icpac-onset`, `sheerwater-icpac-onset`,
`agrhymet-sos`, `agrhymet-sos-rolling`, `sheerwater-chc-onset`, `moron-robertson-2014`,
`sheerwater-moron-robertson-onset`). It **changes no behaviour**: `indicator`'s legacy aliases
resolve to the entries that reproduce exactly what they expanded to before. `chc-onset` is
`sheerwater-chc-onset`; `icpac-onset` is `icpac-onset`. Which column should be canonical where
they differ is a decision for the team, not something this PR makes.

## Shared contract

These are relied on by other code; change them only deliberately.

- **Canonical file:** `registry/onset_definitions.toml`.
- **Consumer copies:** skills are self-contained (run via uvx, installed singly) and cannot
  import `registry/`. A consuming skill ships a byte-identical copy at
  `skills/<skill>/references/onset_definitions.toml`. `python tools/sync_definitions.py`
  writes the copies for every skill in its `CONSUMERS` tuple (LF line endings; a consumer
  whose skill directory is absent on the branch is skipped with a note);
  `python tools/sync_definitions.py --check` exits 1 listing any missing or differing copy.
- **Content hash:** computed over the scientific fields only, so prose edits keep an
  entry's identity and parameter edits change it. Reproduce it inside a skill with:

  ```python
  hashlib.sha256(
      json.dumps(
          {k: d[k] for k in ("time_basis", "trigger", "confirm", "veto", "search") if k in d},
          sort_keys=True,
      ).encode()
  ).hexdigest()[:12]
  ```

  where `d` is the entry's table as parsed by `tomllib`. `tunable`, `optimization` and all
  prose sit outside the hash.
- **Output provenance:** a skill that computes onset from a registered definition records
  these attrs on its output:
  - `onset_definition_id` — the registry key, e.g. `icpac-onset`
  - `onset_definition_hash` — the content hash above
  - `onset_definition_status` — the entry's status, or `unregistered-variant` if any CLI
    override changed a parameter
  - `onset_definition_overrides` — the overridden fields and values (empty when none)

## Where this should live

`weather-skills-core` is probably the long-term home: it already hosts the shared dimension
ontology, and every skill already depends on it, so skills could read the registry as
package data with no copies and no sync step. This draft keeps it in this repo with a sync
tool instead, because the definitions and their drift tests need to change alongside the
skills that consume them while the schema is still settling, and a core release per edit
would slow that down. Once the schema is stable, moving the TOML and `onset.py` into core
retires the copies and `tools/sync_definitions.py`.

## Relation to Sheerwater's event registry

Sheerwater (`rhiza-research/sheerwater`) already has an `EVENT_REGISTRY` in
`sheerwater/interfaces/events.py`: decorator-registered Python event functions such as
`above_threshold`, `below_threshold` and `accumulated_rain`. It also has the
suitable-planting-window onset condition in `sheerwater/tasks/spw.py`. That registry is code, and
it serves the benchmark.

This file is complementary. It is declarative data, readable by self-contained skills and by
agents, with each definition's source, status and content hash. The Sheerwater onset condition is
registered here as `sheerwater-spw-rainy-onset` and compiles exactly to an `indicator` rule.

A natural next step is to generate one from the other: for example, make Sheerwater events
available as registry entries, or have Sheerwater read these definitions. That is a maintainer
decision, and nothing here assumes it.
