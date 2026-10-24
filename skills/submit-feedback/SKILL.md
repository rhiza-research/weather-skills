---
name: submit-feedback
description: "Turn a title and markdown body you write into a prefilled GitHub new-issue link, which the user clicks to file feedback about the skills under their own account. Nothing is sent by the skill. Use when the user asks to report a bug, a suggestion, or other feedback about a skill."
license: MIT
compatibility: Requires Python 3.12 and uv. Builds a URL string only; reads no credentials, makes no network request, and writes no file.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/build_url.py *)
metadata:
  version: "0.0.4"
  catalog-group: agent-tooling
---

# submit-feedback

URL-encodes your report into a GitHub new-issue link and checks the link is short enough for GitHub to accept.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/build_url.py --title TITLE (--body TEXT | --body-file PATH) [--repo NAME]
```

### Arguments
- `--title` — issue title. Required, not empty.
- `--body` — issue body as markdown.
- `--body-file` — read the body from this UTF-8 file instead. Pass exactly one of `--body`, `--body-file`.
- `--repo` — the `rhiza-research` repo that owns the skill: `weather-skills`, `weather-skills-core`, `weather-skills-plotting`, or `chc-skills`. Default: `weather-skills`.

### Output
- Exit 0: the URL alone on stdout (`https://github.com/rhiza-research/<repo>/issues/new?title=…&body=…`); a length note on stderr.
- Exit 1: the URL would be over 6,800 characters. Nothing on stdout; stderr says roughly how many body characters to cut.
- Exit 2: empty title or body, both or neither body source, unreadable or non-UTF-8 input, or a title too long on its own.

### Notes
- Run it only when the user asks for a report, not after an error on your own initiative.
- Write the report yourself: one-line summary, what the user did, what they expected, what happened. Keep the body near 600 words (about 4,000 characters); percent-encoding expands it. The link holds only the title and body you pass, so leave out secrets and personal data.
- Give the user the link as clickable text and tell them to open it, sign in to GitHub if asked, review or edit the issue, and press **Submit new issue**.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/build_url.py --title "plot: empty map for IMERG over Kenya" \
  --body "**Did:** plotted a clipped IMERG Zarr for 2026-10-01 to 2026-10-14.
**Expected:** a filled rainfall map. **Got:** a near-white PNG, colour bar 0 to 0.0001 mm."
```
Prints a `rhiza-research/weather-skills` new-issue link with that title and body filled in.
