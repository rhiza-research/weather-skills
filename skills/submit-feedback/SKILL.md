---
name: submit-feedback
description: Build a prefilled GitHub new-issue link the user clicks to file feedback about the skills under their own GitHub account. You write the title and body; the skill URL-encodes them, checks the link fits GitHub's length limit, and sends nothing itself. Use only when the user asks to report a bug, suggestion, or feedback about a skill — never on your own initiative after an error. Pass --repo for the rhiza-research repo that owns the skill.
license: MIT
compatibility: Requires Python 3.12 and uv. Builds a URL string only; reads no credentials, makes no network request, and writes no file.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/build_url.py *)
metadata:
  version: "0.0.2"
  catalog-group: agent-tooling
---

# submit-feedback

Turn a report you have written into a single clickable GitHub *new issue* link.
The user clicks it, GitHub opens the new-issue form prefilled, and they press
**Submit** to file it under their own account. Nothing is posted until that
human click.

## When to use

- The user hits something broken, confusing, or missing in the skills and wants
  it on record.
- The user asks to file a bug report, a suggestion, or feedback about the skills
  or their output.
- The user says "submit feedback", "report this", "open an issue", or similar.

Do not use it unprompted — not after a failed run, and not to log your own
workarounds — unless the user asks for a report. The skill only builds the
link; the user is the one who clicks and submits.

The link carries only the title and body you pass: no paths, environment,
usernames, or file contents are added. Do not put secrets, credentials, or
personal data in the body.

## Target your input length first

Aim for a body of **about 600 words** — steer by the word count, since that is
what you can actually control while writing. The hard limit is on characters:
GitHub accepts a prefilled URL only up to about 6,800 characters total, and
after percent-encoding (spaces, newlines, and markdown punctuation all expand)
plus the fixed URL overhead, ~4,000 characters of body — roughly 600 words — is
the comfortable ceiling. The skill enforces the character limit; you just keep
the body short. Compose within that budget the first time rather than writing
long and trimming. If you do exceed it, the skill tells you approximately how
many characters to cut.

So: write a focused report — a one-line summary, what the user was doing, what
they expected, what happened, and at most a turn or two of relevant context.
Quote selectively; do not paste the whole conversation.

## You author the content; the skill only formats and checks it

This skill does not decide what the report says. You compose the title and body
from the conversation; the skill percent-encodes them, assembles the URL, and
verifies it fits.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/build_url.py --title <title> (--body <text> | --body-file <path>) \
    [--repo NAME]
```

Issues go to a `rhiza-research` repository: by default
`weather-skills`. When the report is about a skill installed from another
rhiza-research skill repo, pass `--repo` with the repo that owns that skill
(its name is in the skill's install source or README) so the issue lands in
the right tracker. Allowed: `weather-skills`, `weather-skills-core`,
`weather-skills-plotting`, `chc-skills`.

### Arguments
- `--title` — the issue title. Required, must not be empty.
- `--body` — the issue body as a markdown string. Must not be empty.
- `--body-file` — a path to read the body from instead. Exactly one of `--body`
  or `--body-file` is required.
- `--repo` — the rhiza-research repository that owns the skill the report is
  about. Default `weather-skills`. Any repo outside the allow-list is an
  input error.

## Output and exit codes

- **Within budget (exit 0):** the assembled URL is printed alone to stdout, so
  you can relay it verbatim. A guidance line goes to stderr.
- **Body too long (exit 1):** no URL is printed; stderr reports the assembled
  length, the limit, how far over it is, and roughly how many characters to cut
  from the body. Shorten the body and run again.
- **Input error (exit 2):** an empty title or body, both or neither body
  source, a title or body that is not valid UTF-8, a missing / unreadable
  `--body-file`, a `--repo` outside the
  allow-list, or a title so long it alone exceeds the URL limit. The reason goes to stderr.

## What to tell the user

After the skill returns a URL, present it to the user as a clickable link and
tell them to:

1. Click it (they will be asked to sign in to GitHub if they are not already).
2. Review the prefilled title and body, editing anything they want.
3. Press **Submit new issue**.

The issue is filed under their own GitHub account, on the target repository.

## Example

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/build_url.py \
    --title "plot produced an empty map for IMERG over Kenya" \
    --body "**What I did:** ran plot on a clipped IMERG zarr.

**Expected:** a filled heatmap.

**Got:** an almost-white PNG; legend range 0 to 0.0001 mm."
```

This prints the clickable issue URL to stdout and the user-facing instructions
to stderr.
