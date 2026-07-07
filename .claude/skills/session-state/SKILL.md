---
name: session-state
description: STATE.md session bookends for this repo. Run at session START to brief from STATE.md and detect drift against git, and at session END to update STATE.md's five sections in place. Use when a session begins in this repo, when the user says "end session", "update state", "where were we", or "brief me", and before finishing any session that changed code.
---

# session-state

`STATE.md` is this repo's project memory: it lets a session resume instead of restarting
from zero, but only if it is read at the start and updated honestly at the end. This skill
runs either bookend. Pick the mode from context: a fresh session or "where were we" →
**start**; "end session" / work is wrapping up → **end**.

STATE.md has exactly five sections. Edit them **in place** — never append new ad-hoc
sections, never turn it into a narrative log:

1. `## Verified facts` — confirmed, stop re-deriving
2. `## General rules` — consult before re-deriving
3. `## Open failures / threads` — investigate next
4. `## Lessons learned` — distilled, apply beyond the specific case
5. `## Last session` — resume pointer

## Mode: start

1. Read `STATE.md` in full.
2. Collect ground truth and compare against it:
   ```bash
   git log --oneline -10
   git status --short
   git branch --show-current
   gh pr list --state open --json number,title,headRefName 2>/dev/null
   ```
3. **Drift check** — git is the source of truth, STATE.md is the map. For each claim in
   "Verified facts" and the resume pointer that git can confirm or refute (merged? branch
   exists? PR open?), verify it. If STATE.md is stale, fix it now (this is exactly how a
   past stale entry — "M0/M1 still unmerged" — cost real time).
4. Brief the user in **at most 10 lines**: where the last session stopped, the concrete
   next action from the resume pointer, any open threads relevant to today, and any drift
   you corrected.

## Mode: end

1. Gather the session's evidence — do not work from memory:
   ```bash
   git log --oneline main..HEAD    # or the session's merged/pushed commits
   git status --short
   ```
   plus test results and PR numbers from this session.
2. Read `STATE.md` in full, then edit the five sections in place:
   - **Verified facts** — promote anything newly *verified this session* (a merge you saw,
     a behavior you tested, a command you ran). The gate is verification, not confidence:
     if you didn't check it this session, it doesn't get promoted.
   - **General rules** — only touch if the maintainer set a new standing rule this session.
   - **Open failures / threads** — add new blockers/questions with enough context to pick
     up cold; remove threads that were closed this session (say how they closed).
   - **Lessons learned** — add only lessons that generalize beyond the specific incident;
     one or two lines each.
   - **Last session** — prepend a new dated entry (keep prior entries below it): what
     happened, current status (PR #, CI state, test counts), and a **concrete next
     action** ("Next: …"). No vague endings like "continue working on X".
3. Keep it clean:
   - No secrets, no request/response bodies, no API keys — same rule as the gateway.
   - Convert relative dates ("yesterday", "next week") to absolute dates.
   - Trim: if a "Last session" entry is more than ~4 entries old and its facts are already
     promoted or stale, delete it. STATE.md is a resume pointer, not an archive.
4. Show the user a short diff summary of what changed in STATE.md.

## What does NOT go in STATE.md

- Anything derivable from the code, `git log`, or `CLAUDE.md` (stable conventions live
  there, not here).
- Session play-by-play. Distill to facts, threads, lessons, and one resume pointer.
