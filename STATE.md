# STATE.md · tokenwarden

Project memory for this repo. **Read at session start; update before ending a session.**
CLAUDE.md holds stable conventions; this file accumulates the things that change —
verified facts, open threads, and a resume pointer. Committed and human-readable; **keep
secrets and request/response bodies out** (same rule as the gateway itself).

Part of the Fable 5 compound-framework STATE.md pilot (`~/Projects/fable5-compound-framework.md`).

## Verified facts — confirmed, stop re-deriving
- M0/M1 (config schema + metering gateway), M3 (daily budgets + threshold alerting), and
  M4 (packaging: pinned deps, CI, LaunchAgent, Makefile) are all **merged to `main`**
  (M0/M1 via PR #1, `e0643f7`; verified 2026-07-05). Older memory that said "M0/M1 is still
  on an unmerged branch" was stale — the project is well past that.
- Opt-in 429 enforcement for over-budget requests is merged (`9263e00`).
- The gateway is **fail-open**: metering must never break or delay the agent's upstream call.
- Default port is **8788** — 8787 collides with the Headroom proxy (`ec509f5`).
- Environment: Python 3.13 venv (`make install`); tests via pytest (7 files under `tests/`).
- tokenwarden meters **metadata only** (tokens, agent, model, cost) — never prompt/response
  bodies, never the API key. Pricing is source-of-truth in `pricing.py` / config.

## General rules — consult before re-deriving
- Never persist or log secrets or request/response bodies. The API key is passthrough-only.
- Keep the proxy fail-open; a metering failure must not block or delay the upstream call.
- When Anthropic prices change, update `pricing.py` / config — never hardcode numbers elsewhere.
- Branch + PR for every change; never push to `main`.
- `tokenwarden.db` and `config.toml` are local, git-ignored state.

## Open failures / threads — investigate next
- **Fable 5 refusal-fallback billing is not yet modeled.** Per the compound framework
  (Rule 6): a Fable 5 request that a safety classifier declines returns HTTP 200 with
  `stop_reason: "refusal"`. A *pre-output* refusal is **unbilled**; a request *rescued* by
  the fallback model bills at the **fallback model's** rates (e.g. Opus 4.8 $5/$25, not
  Fable's $10/$50). Correct per-agent attribution must read `usage.iterations`, not assume
  the requested model served the response. → Verify tokenwarden's current metering against a
  real refused-then-rescued Fable call **before** scaling any Fable automation.
- Public-release prep is in flight: remote branches `chore/genericize-for-public` and
  `docs/auto-refresh-20260619` exist; PR #4 genericized internal references. The status of
  the public cut is not yet confirmed here.

## Lessons learned — distilled, apply beyond the specific case
- Local-proxy port collisions fail silently and cost real debugging time: pick a default
  that can't overlap other local proxies (Headroom holds 8787 → tokenwarden defaults to 8788).

## Last session — resume pointer
- **2026-07-07** · **CLAUDE.md rewritten as a self-contained operating manual** (invariants
  table, 12-mistake catalog with preventing rules, checkable quality bars per deliverable,
  exact escalation rules) + three repo skills added under `.claude/skills/`:
  `tokenwarden-ship` (pre-flight → PR → CodeRabbit triage loop), `session-state`
  (STATE.md bookends), `price-sync` (sourced price-table updates). All documented commands
  executed and verified this session; `make test` = 62 passed, 1 skipped. On branch
  `docs/claude-md-operating-manual`. **Next:** triage CodeRabbit on the PR, then the
  maintainer merges.
- **2026-07-06 (later)** · **PR #6 SHIP phase.** All 4 CodeRabbit actionable comments
  fixed + resolved (`33e5347`); follow-up `b0bd079` made the `--notify` delivery summary
  honest (delivered/failed counts, not "sent N" on failure), typed `Alert.kind` as a
  `Literal` (closed the last nitpick), and strengthened the flaky-channel test. CI green
  on 3.11/3.12/3.13, 61 passed + 1 skipped, `test_gateway_import_stays_torch_free` holds.
  **Benchmark still blocked:** `tokenwarden.db` has 0 events — the gateway has recorded
  no real traffic, so `scripts/forecast_benchmark.py` has nothing to backtest. Run the
  gateway for ≥2 days of real hourly history first. **Next:** James's approval → merge
  PR #6. (Quiz-gate blocker retired 2026-07-06: /quiz-me is on-request learning, never a
  merge blocker.)
- **2026-07-06** · Built the **TimesFM spend-forecasting** feature on branch
  `feat/timesfm-forecasting` (F0→F4). New `tokenwarden forecast` command projects
  today's end-of-day spend with quantile bands (warns on projected overrun before it
  lands) and flags runaway-agent anomalies (spend above the forecast band). Two backends
  behind a `Forecaster` protocol: stdlib seasonal-naive baseline (always on) + optional
  zero-shot **TimesFM 2.5** (`tokenwarden[forecast]` extra, torch). Key invariant:
  forecasting is a **separate offline DB reader** — `gateway.py` imports neither
  `forecast.py` nor torch (test-enforced: `test_gateway_import_stays_torch_free`). New:
  `src/tokenwarden/forecast.py`, `scripts/forecast_benchmark.py` (naive-vs-TimesFM
  backtest = the `[ASTGL CONTENT]` benchmark). 57 tests + 1 skipped (TimesFM smoke,
  gated by `importorskip`). **Next:** open the PR, triage CodeRabbit, and once the gateway
  has ≥2 days of real hourly history, run `scripts/forecast_benchmark.py` with the
  `forecast` extra installed to get the real TimesFM-vs-naive numbers for the post.
- **2026-07-05** · STATE.md scaffolded as the first repo in the Fable 5 compound-framework
  STATE.md pilot. No code changed. Next: (a) exercise the Fable refusal-billing open item
  above when the framework's metering work begins; (b) confirm the public-release cut status.
