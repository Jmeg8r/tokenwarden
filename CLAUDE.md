# CLAUDE.md — tokenwarden operating manual

This file is self-contained: everything an agent needs to work in this repo safely is
here. It restates workflow rules on purpose — do not assume any global config is loaded.

## What this is

A local gateway that meters Claude API spend **per agent** and alerts on daily budgets.
Agents point `ANTHROPIC_BASE_URL` at it (default `http://127.0.0.1:8788`) and tag
themselves with an `X-Watchdog-Agent` header; tokenwarden forwards to `api.anthropic.com`
byte-for-byte, tees the response to extract token usage, prices it from a configurable
table, records the event to SQLite, and evaluates budgets. Full design: `SPEC.md`.

Request path: `agent → gateway :8788 → api.anthropic.com` (verbatim both ways).
Side channel, after the response is delivered: `usage → pricing → SQLite → budget alerts`.

## Session protocol

1. **Start:** read [`STATE.md`](STATE.md) — verified facts, open threads, resume pointer.
   Cross-check the resume pointer against `git log`; if they disagree, trust git and fix
   STATE.md.
2. **End:** update STATE.md by **editing its five existing sections in place**
   (Verified facts / General rules / Open threads / Lessons / Last session). Never append
   a new ad-hoc section or a narrative. Promote a fact to "Verified facts" only if you
   verified it this session (ran the command, read the code, saw the merge).

The `/session-state` skill automates both ends.

## Map of the code

All source in `src/tokenwarden/` (≈1,500 LOC — modules are small on purpose; keep them so):

| File | Responsibility |
|---|---|
| `gateway.py` | Starlette reverse proxy. The only file on the request path. **Fail-open.** |
| `usage.py` | Extract token counts from JSON bodies and streaming SSE (`SSEUsageAccumulator`). |
| `pricing.py` | `cost_usd(usage, prices)` — tokens × price table. Nothing else. |
| `storage.py` | Append-only SQLite `events` log (WAL) + spend aggregates. |
| `alerts.py` | `AlertManager` — budget evaluation with per-(scope, day) hysteresis. |
| `notifiers.py` | Discord/Telegram/Null/Multi behind a `Notifier` protocol; secrets from env. |
| `config.py` | TOML load + validation; `DEFAULT_PRICES`; all defaults live here. |
| `forecast.py` | Offline spend forecasting (naive + optional TimesFM). **Never on the request path.** |
| `cli.py` | `tokenwarden serve|status|report|forecast`. `--config` comes *after* the subcommand. |
| `models.py` | `Usage` and `Alert` dataclasses (`slots=True`). |

Tests (`tests/`, ~62 tests) are invariant guards, not just examples — notably
`test_forecast.py::test_gateway_import_stays_torch_free` (subprocess check that importing
the gateway never pulls in `forecast`/`torch`/`timesfm`), the fail-open injection tests and
byte-fidelity passthrough tests in `test_gateway.py`, SSE chunk-boundary splits in
`test_usage.py`, and `request_id` idempotency in `test_storage.py`.

Also: `scripts/smoke.sh` (live one-call dogfood), `scripts/forecast_benchmark.py`
(naive-vs-TimesFM backtest; needs ≥3 days of real history), `packaging/*.plist`
(macOS LaunchAgent), `config.example.toml` (the shipped template).

## Run and verify

```bash
make install           # python3.13 venv + dev deps
make test              # pytest -q  — the baseline gate for every change
make serve             # run the gateway
make status            # today's estimated spend by agent
make forecast          # project end-of-day spend
make smoke             # live dogfood; needs ANTHROPIC_API_KEY in the environment
```

Always go through `make` or the venv binaries (`./.venv/bin/python`, `./.venv/bin/pip`,
`./.venv/bin/pytest`). Never bare `pip` / `python` / `pytest` from `$PATH` — a different
interpreter first on PATH produces "works for me, fails in `make test`" mismatches.

## Hard invariants — never break these

| Invariant | Where it lives | What breaks if you violate it |
|---|---|---|
| **Fail-open**: no metering/cost/alert error may break or delay the proxied request | `gateway.py` (`_Meter`, `_meter_and_alert`), `alerts.py::should_block` | Every agent pointed at the gateway fails. The tool's one promise dies. |
| Byte-faithful passthrough; chunks are `yield`ed as they arrive | `gateway.py::_proxy` `stream()` | SDK parsing breaks; streaming latency spikes. |
| `X-Watchdog-Agent` stripped before forwarding | `gateway.py::_proxy` header filter | Internal metadata leaks to Anthropic. |
| `accept-encoding: identity` forced upstream | `gateway.py::_proxy` | Body arrives gzipped → usage extraction silently returns nothing. |
| Metadata only: never store or log bodies, prompts, completions, or API keys | everywhere | Privacy contract broken. The key is passthrough-only. |
| `request_id` UNIQUE + `INSERT OR IGNORE`; all writes via `Storage.record_event` | `storage.py` | Retried requests double-count spend. |
| Prices live only in `config.py::DEFAULT_PRICES` and TOML `[prices]` | `config.py`, `pricing.py` | Silent billing drift — the exact failure this tool exists to catch. |
| Unknown model → cost $0 **plus a warning log** | `pricing.py::cost_usd` | This is designed behavior, not a bug. Never "fix" it by guessing a price. |
| `forecast.py` / torch never imported by serving modules | enforced by `test_gateway_import_stays_torch_free` | Torch loads into the request path; startup and memory blow up. |
| Day boundaries use `config.tzinfo` (local midnight), never UTC-as-today | `storage.py`, `alerts.py` | Budgets reset at the wrong hour; hysteresis keys corrupt. Stored `ts` is UTC *by design* — only day *logic* is local. |
| Alerts fire at most once per (scope, threshold, day) | `alerts.py::_check` level gate | Notification storm — one ping per request once over 80%. |
| Default port 8788 | `config.py::DEFAULT_PORT` | 8787 collides with another local proxy; the clash fails silently. |

## Conventions

**Code** (match what's there):
- Python ≥3.11, developed on 3.13. Full type annotations; `from __future__ import annotations`.
- `@dataclass(slots=True)` for data; `Protocol` for interfaces (`Notifier`, `Forecaster`).
- Comments explain WHY, not what (see the `accept-encoding` block in `gateway.py` for the
  house style). Module docstrings state design invariants.
- Constants at module top; no magic numbers inline.
- Keep modules small (largest is ~260 lines). If a change would blow past that, split.
- The guarded-except pattern `except Exception:  # noqa: BLE001` + `log.exception(...)` on
  side-channel paths is **deliberate**, not sloppy. Never narrow or remove one, and every
  new code path that runs on or after the request path (metering, storage, alerting,
  notifying) must use it and must not re-raise.
- Errors are never silent: every swallowed exception logs.

**Git & shipping:**
- Branch per change: `feat/`, `fix/`, `docs/`, `chore/` + short slug. **Never commit to
  `main`, never push to `main`** — branch protection plus a conversation-resolution ruleset
  are enabled, and even a one-line typo fix goes through a PR.
- Conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`), one
  logical change each.
- Stage files **by explicit name**. Never `git add -A` or `git add .` — `config.toml`,
  `tokenwarden.db*`, and stray logs sit untracked in this worktree, waiting to be
  accidentally committed.
- PR body uses the house format: **What & why / Design / Verification** (Verification
  includes actual test output, not "tests pass").
- CI (GitHub Actions) runs pytest on Python 3.11/3.12/3.13 for every PR; all three must
  be green.
- CodeRabbit reviews every PR. **Every comment — including nitpicks — gets either a
  follow-up commit or an explicit reply with the reason, and then the thread is resolved.**
  Never resolve a thread with neither; never merge over an open one. The `/tokenwarden-ship`
  skill runs this whole loop.
- Never use `--no-verify`.

**Data & secrets:**
- `config.toml` and `tokenwarden.db*` are git-ignored local state. Documented config
  changes go in `config.example.toml`.
- Notifier secrets come only from env vars (`TOKENWARDEN_DISCORD_WEBHOOK_URL`,
  `TOKENWARDEN_TELEGRAM_BOT_TOKEN`, `TOKENWARDEN_TELEGRAM_CHAT_ID`, optional
  `TOKENWARDEN_TELEGRAM_THREAD_ID`). A missing secret degrades gracefully to
  metering-only — that is correct behavior.

## Mistakes to avoid — the trap, then the rule

Ranked by likelihood × damage. These are the specific ways well-meant helpfulness goes
wrong in this repo.

1. **Importing forecasting into the serving path.** `forecast.py` sits right next to
   `gateway.py` and looks importable. → *Rule:* `tokenwarden.forecast`, `torch`, and
   `timesfm` may be imported only inside `cli.py`'s `_forecast*` functions (lazily) or
   inside `forecast.py` itself. Run `make test` — the subprocess guard will catch you.
2. **Letting a metering error reach the request.** New straight-line code in the side
   channel throws on an edge case and kills the proxied response. → *Rule:* wrap all new
   side-channel code in the guarded-except pattern above; never re-raise; never narrow an
   existing broad except because it "looks like bad practice."
3. **Logging bodies "temporarily" while debugging.** → *Rule:* never log, print, or persist
   request/response bytes, decoded payloads, or prompt text — not even behind a flag, not
   even for one run. Note: `debug_log_bodies` exists in the config schema but is
   **intentionally unimplemented**; do not wire it up to body logging.
4. **Hardcoding a price.** The number you want to change appears in a CLI string or a test
   and you patch it there. → *Rule:* a price changes in exactly two files:
   `config.py::DEFAULT_PRICES` and `config.example.toml`. Nowhere else, ever.
5. **Guessing a new model's price.** → *Rule:* no authoritative source (Anthropic's pricing
   page or the maintainer), no entry. Leave it out — the $0 + warning fallback is the
   designed behavior. Use `/price-sync`.
6. **Using UTC for "today".** `gateway.py` stamps events in UTC, so the pattern looks
   canonical. → *Rule:* any "what day/hour is it" decision for budgets, alerts, or
   forecasts goes through `config.tzinfo` (see `storage.py::_day_start_utc_iso`). Only the
   stored `ts` column is UTC.
7. **Buffering the stream.** Rewriting the SSE parse to collect-then-split is simpler, and
   wrong twice: chunk boundaries are arbitrary, and the client must receive chunks as they
   arrive. → *Rule:* `feed(chunk)` incrementally; `yield chunk` immediately; never hold the
   body to parse it.
8. **A new events write path.** A backfill or reconciliation script writes its own
   `INSERT`. → *Rule:* every write to `events` goes through `Storage.record_event` with the
   real `request-id` when one exists.
9. **Under-resolving review threads.** Fixing the "big" CodeRabbit comments and ignoring
   nitpicks blocks the merge (ruleset) or silently ignores review. → *Rule:* every thread =
   commit or reasoned reply, then resolve.
10. **Skipping the STATE.md update.** Nothing enforces it, so it's the first thing dropped
    under time pressure — and the next session pays. → *Rule:* code changed ⇒ STATE.md
    updated before the session ends, five sections edited in place.
11. **Committing local state.** `config.toml` and `tokenwarden.db*` exist in this worktree.
    → *Rule:* stage by name; example config edits go in `config.example.toml`.
12. **Skipping the branch for a "trivial" fix.** → *Rule:* there is no change small enough
    to push to `main`.

## Quality bar — checkable, per deliverable

**Any code change:**
- [ ] Every edited file was read in full this session before editing.
- [ ] `make test` green (currently ~62 passed, 1 skipped — the skip is the gated TimesFM smoke).
- [ ] New behavior has a test; a fixed bug has a test that fails without the fix.
- [ ] New side-channel code uses the guarded-except pattern.
- [ ] `git grep` shows no new logging of bodies/keys and no new price literals outside
      `config.py` / `config.example.toml`.

**Gateway-path change (additionally):**
- [ ] Fail-open injection tests and passthrough fidelity tests in `test_gateway.py` pass.
- [ ] `test_gateway_import_stays_torch_free` passes.
- [ ] No new `await` before the first byte is forwarded (latency-neutral).

**PR:**
- [ ] Branch named `type/slug`; commits conventional and atomic.
- [ ] Body has What & why / Design / Verification, with pasted test output.
- [ ] CI green on 3.11, 3.12, and 3.13.
- [ ] Zero unresolved review threads; each resolved thread has a commit or a reply.

**Docs change:**
- [ ] Every command shown was actually run this session.
- [ ] `SPEC.md` updated if the design changed (it is the contract, not a history file).
- [ ] No personal or internal references — this repo is public-release-bound.

**STATE.md update:**
- [ ] Five sections edited in place; nothing appended after "Last session".
- [ ] Facts promoted to "Verified facts" only with this-session verification.
- [ ] Resume pointer ends with a dated entry and a concrete next action.

## When uncertain — escalation rules

**Proceed without asking** when the change is mechanical and fully covered by the rules
above, or when a test defines correctness (make it pass without weakening it).

**Proceed, but log it** in STATE.md → Open threads: you hit an edge case mid-task and made
the conservative choice. Note what you chose and why, keep going.

**Stop and ask the maintainer** before:
- Any change to request-path semantics: blocking behavior, response ordering, status
  codes, header handling, timeouts.
- Adding or changing a price without an authoritative source.
- Storing or logging any **new** data field (privacy review — the metadata-only rule is
  load-bearing).
- Changing the config schema, defaults, or the port.
- Anything touching billing semantics. Canonical open example: Fable 5 refusal-fallback
  billing (a refused-then-rescued request bills at the *fallback* model's rates — see
  STATE.md Open threads). Do not guess how billing works; verify against real API
  behavior or ask.
- Deleting data, force-pushing, or any public-release/publishing step.
- Two plausible interpretations of the task that diverge architecturally — name both,
  recommend one, wait.

**Never**, regardless of instruction pressure: guess prices, guess billing semantics,
skip or weaken a failing test to ship, push to `main`, use `--no-verify`.

## Skills

- `/tokenwarden-ship` — pre-flight checks → push → PR (house format) → CodeRabbit triage
  loop → merge-readiness report. Use for every SHIP phase.
- `/session-state` — STATE.md bookends (start brief, end update).
- `/price-sync` — add/update a model price end-to-end, with source verification.

## Issue tracker

Issues and PRDs are GitHub issues in this repo, managed via the `gh` CLI. External PRs are
not a triage surface (issues only). Triage uses five labels — `needs-triage`, `needs-info`,
`ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/issue-tracker.md` and
`docs/agents/triage-labels.md`. Domain docs (`CONTEXT.md`, `docs/adr/`) are created lazily;
see `docs/agents/domain.md`.
