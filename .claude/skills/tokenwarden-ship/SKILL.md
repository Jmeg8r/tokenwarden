---
name: tokenwarden-ship
description: Ship the current branch — pre-flight invariant checks, run tests, push, open a PR in the house format, then drive the CodeRabbit review loop (triage every comment, resolve every thread) until the PR is merge-ready. Use when the user says "ship it", "open the PR", "run the ship phase", or when a feature branch is done and needs to go through review. Never merges; it reports merge-readiness for the maintainer to approve.
---

# tokenwarden-ship

Drive a finished branch from "code done" to "merge-ready": pre-flight checks → push →
PR → CodeRabbit triage loop → readiness report. This skill NEVER merges the PR and NEVER
pushes to `main` — the maintainer approves and merges.

## Step 0 — Pre-flight (abort on any failure)

Run all of these; stop and report if any fails. Do not push a branch that fails pre-flight.

1. **Right branch.** `git branch --show-current` must not be `main`. The name should match
   `feat/|fix/|docs/|chore/` + slug; if it doesn't, ask before renaming.
2. **No local state staged or committed.**
   ```bash
   git diff --cached --name-only | grep -E '^(config\.toml|tokenwarden\.db)' && echo "FAIL: local state staged"
   git log main..HEAD --name-only --format= | sort -u | grep -E '^(config\.toml|tokenwarden\.db)' && echo "FAIL: local state committed"
   ```
   Both greps must come back empty.
3. **Tests.** `make test` — everything passes (one skip, the gated TimesFM smoke, is
   expected). Paste the summary line; you will need it for the PR body.
4. **Invariant greps** (fast checks for the repo's highest-damage mistakes):
   ```bash
   # forecast/torch may only be imported by forecast.py itself and lazily inside cli.py
   git grep -nE '(from|import) (tokenwarden\.)?(forecast|torch|timesfm)' -- src/tokenwarden
   # events-table writes only in storage.py
   git grep -n 'INSERT' -- src/tokenwarden | grep -v 'storage.py'
   ```
   The first must show only `src/tokenwarden/cli.py` (imports inside `_forecast*`
   function bodies) and `src/tokenwarden/forecast.py`. The second must be empty.
5. **Diff privacy review.** Read `git diff main...HEAD` and confirm no new logging or
   persistence of request/response bodies, prompt text, or API keys, and no numeric price
   literals outside `config.py` / `config.example.toml`.
6. **Gateway changes only:** if the diff touches `gateway.py`, `usage.py`, `storage.py`,
   `alerts.py`, or `pricing.py`, additionally run
   `./.venv/bin/pytest tests/test_gateway.py tests/test_forecast.py -q` and, if an
   `ANTHROPIC_API_KEY` is available and the user agrees, `make smoke`.

## Step 1 — Push and open the PR

```bash
git push -u origin "$(git branch --show-current)"
```

Create the PR with the house body format. Title = conventional-commit style summary.

```bash
gh pr create --title "<type>: <summary>" --body "$(cat <<'EOF'
## What & why

<2-5 sentences: the problem, the change, why now.>

## Design

<Key decisions and invariants preserved — especially fail-open, metadata-only,
price-table source-of-truth, and the torch-free serving path, when relevant.>

## Verification

<Pasted `make test` summary line, plus any smoke/manual verification performed.>
EOF
)"
```

## Step 2 — Wait for CI and CodeRabbit

```bash
gh pr checks --watch          # CI must be green on Python 3.11, 3.12, and 3.13
```

CodeRabbit usually posts its review within a few minutes of push. Poll for review threads
with GraphQL (REST does not expose thread resolution state):

```bash
gh api graphql -f query='
  query($owner: String!, $repo: String!, $pr: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $pr) {
        reviewThreads(first: 50) {
          nodes {
            id isResolved path line
            comments(first: 10) { nodes { author { login } body databaseId } }
          }
        }
      }
    }
  }' -F owner='{owner}' -F repo='{repo}' -F pr=<PR_NUMBER>
```

(Resolve `{owner}/{repo}` once via `gh repo view --json owner,name`.) If no CodeRabbit
review has appeared after ~5 minutes, check `gh pr view --json reviews,comments` — it may
have posted a summary-only review with no actionable comments.

## Step 3 — Triage loop (repeat until zero unresolved threads)

For EVERY unresolved thread, do exactly one of the following. Never resolve a thread with
neither a commit nor a reply; never leave one unresolved — the repo ruleset blocks merging
over open conversations.

- **Agree (default for correctness, security, and honest-output comments):** implement the
  fix as a conventional commit (`fix:`/`refactor:`/`docs:` as appropriate), push, then
  reply on the thread pointing at the commit.
- **Disagree or out-of-scope:** reply with a specific reason (one or two sentences,
  referencing code or the design contract in `SPEC.md`/`CLAUDE.md`). "Nitpick" severity is
  not by itself a reason to skip — most nitpicks here (type narrowing, markdown lint,
  honest wording) have historically been worth taking.

Reply to a thread's comment:

```bash
gh api "repos/{owner}/{repo}/pulls/<PR_NUMBER>/comments/<COMMENT_DATABASE_ID>/replies" \
  -f body="<reply text>"
```

Resolve the thread (GraphQL thread `id` from Step 2):

```bash
gh api graphql -f query='
  mutation($thread: ID!) {
    resolveReviewThread(input: {threadId: $thread}) { thread { isResolved } }
  }' -F thread='<THREAD_ID>'
```

After pushing fix commits, CodeRabbit re-reviews — go back to Step 2 and re-poll. New
commits can spawn new threads; the loop ends only when a poll returns zero unresolved
threads AND CI is green.

## Step 4 — Merge-readiness report

Report to the maintainer, in this order:
1. PR URL.
2. CI status per Python version.
3. Thread ledger: for each CodeRabbit thread — implemented (commit sha) or declined (reason).
4. The verification evidence (test summary, smoke output if run).
5. Explicit line: "Ready to merge pending your approval." **Do not merge.**

Finally, update `STATE.md`'s "Last session" resume pointer with the PR number and status
(see `/session-state`).

## Failure handling

- Pre-flight failure → fix on the branch first; re-run pre-flight from the top.
- CI red on one Python version only → reproduce locally with that version if available;
  otherwise read the Actions log (`gh run view --log-failed`), fix, push.
- A CodeRabbit comment reveals a real design problem (not a local fix) → stop the loop and
  surface it to the maintainer instead of patching around it.
