# Spec — tokenwarden (Claude API Spend Watchdog)

> A lightweight, language-agnostic gateway that meters Claude API credit
> consumption **per agent**, alerts when daily spend approaches a budget, and
> logs every billing event to SQLite. Built standalone/open-source; ClaudeClaw
> is the first consumer.

**Status:** M0/M1 implemented on branch `feat/m0-m1-gateway`; M2+ pending
**Last updated:** 2026-06-16
**Name:** `tokenwarden` (decided 2026-06-16). Repo at `~/Projects/tokenwarden`.
ClaudeClaw is James's internal platform and the first consumer, not the product.

---

## 1. Problem & context

Anthropic's billing for programmatic/Agent-SDK usage is actively in flux. On
2026-05-14 Anthropic announced a split moving Agent SDK / `claude -p` /
third-party usage off the subscription pool into a separate monthly credit
(effective 2026-06-15); on 2026-06-15 they **paused it on the day it was due** —
the third reversal in this area since January. Whatever the next change is, the
risk is the same: **surprise charges, and tooling that breaks because it
hardcoded billing assumptions.**

This tool defends against that for any fleet running Claude through API keys —
ClaudeClaw first, but useful to anyone on the Agent SDK / Messages API.

### Verified facts this design rests on (Anthropic docs, 2026-06-16)
- Auth determines billing: `sk-ant-api03-` keys → prepaid credits at token
  rates (what the Agent SDK docs require for production). `sk-ant-oat01-`
  subscription tokens → subscription limits, **no per-token dollars**.
- **There is no API for credit balance, spend limits, or low-balance webhooks.**
  You must accumulate spend yourself → a self-imposed **budget** is the only
  workable trigger.
- The Admin Cost/Usage API exists and is ~5 min fresh with per-minute buckets,
  but: requires an admin key + an Organization + owner/admin role (a real OSS
  adoption barrier), and **authoritative dollars (`cost_report`) only break down
  by workspace, not by API key.** → phase 2, not MVP.

---

## 2. Goals / non-goals

**Goals (MVP)**
- Meter per-agent token usage in near-real-time without modifying agent code.
- Compute estimated cost from a **configurable** price table.
- Enforce nothing by default; **alert** on per-agent and global daily budgets.
- Persist every event to SQLite (append-only, auditable).
- Pluggable alert channel (Discord + Telegram); never hardwire one.

**Non-goals (MVP)**
- Authoritative dollars / reconciliation against Anthropic's Cost API (phase 2).
- Monitoring Managed Agents internal model calls (the loop runs Anthropic-side;
  not visible to a base-URL gateway — phase 2 via Admin API / session usage).
- Monitoring subscription-token (`oat01`) agents' dollars (none exist; track
  rate-limit headroom instead — phase 2).
- A web dashboard (phase 2; ClaudeClaw `/pmo` can read the SQLite directly).

---

## 3. Architecture

```
   agent (any language/SDK)
        │  ANTHROPIC_BASE_URL=http://localhost:8787
        │  header: X-Watchdog-Agent: forge
        ▼
 ┌─────────────────────────────┐        ┌──────────────────────┐
 │   Watchdog Gateway (daemon)  │ ─────▶ │ api.anthropic.com     │
 │  • transparent passthrough   │ ◀───── │ (configurable upstream)│
 │  • tee response, read usage  │        └──────────────────────┘
 │  • cost = tokens × pricetable│
 │  • write event → SQLite      │
 │  • eval budgets → notify     │
 └──────────────┬───────────────┘
                │
        ┌───────┴────────┐         ┌──────────────────┐
        ▼                ▼         ▼                  ▼
   SQLite (WAL)     Notifier   Discord webhook   Telegram bot
   events log       (iface)
```

### 3.1 Gateway (the core)
A long-running local reverse proxy. Agents point `ANTHROPIC_BASE_URL` at it.

- **Transparent passthrough.** Forward method, path, query, body, and auth
  headers verbatim to the upstream; return upstream status/headers/body verbatim.
  Strip `X-Watchdog-*` headers before forwarding.
- **Key handling.** The agent keeps sending its own `x-api-key`; the gateway
  only forwards it. The gateway stores **no** API keys → smaller blast radius.
- **Streaming.** Must tee SSE without buffering the whole body (latency). Pass
  bytes straight through while incrementally parsing events to capture usage:
  - `message_start` → `input_tokens`, `cache_creation_input_tokens`,
    `cache_read_input_tokens`, `model`.
  - `message_delta` → final `output_tokens`.
  - Non-streaming → parse `.usage` from the JSON body.
- **Attribution.** `agent_id` comes from the `X-Watchdog-Agent` request header.
  Fallback order: header → hash of the API key id → `"unattributed"`.
- **FAIL-OPEN (prime directive).** Any error in metering/cost/alerting MUST NOT
  affect the proxied request. The agent always gets its response. Metering is
  best-effort and side-channel. This is a hard requirement, with tests.
- **Privacy.** Never log prompt/completion bodies by default — usage metadata
  only. A debug body-logging flag exists but defaults off.
- **Scope assumption:** agents and gateway are co-located (localhost, HTTP).
  Remote agents (TLS on the gateway) is out of MVP scope.

### 3.2 Storage — SQLite (WAL mode)
Append-only `events` table; aggregates computed on read (add rollup table only
if/when query perf demands it).

```sql
CREATE TABLE events (
  id            INTEGER PRIMARY KEY,
  ts            TEXT NOT NULL,            -- UTC ISO-8601
  agent_id      TEXT NOT NULL,
  model         TEXT,
  service_tier  TEXT,
  input_tokens          INTEGER NOT NULL DEFAULT 0,
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
  cost_usd      REAL NOT NULL,           -- ESTIMATED (price table × tokens)
  request_id    TEXT UNIQUE,             -- from response `request-id`; idempotency
  source        TEXT NOT NULL DEFAULT 'gateway'
);
```
- `request_id` UNIQUE → dedupe on retries/reconnects (don't double-count).
- All stored dollars are **estimates** until phase-2 reconciliation; label them
  as such everywhere they surface.

### 3.3 Cost engine
- `cost_usd = Σ(token_bucket × rate_from_config)`.
- Default price table (per MTok; cache_read ≈ 0.1× input, cache_write ≈ 1.25× input):
  - Opus 4.8: in 5.00 / out 25.00 / cache_read 0.50 / cache_write 6.25
  - Sonnet 4.6: in 3.00 / out 15.00 / cache_read 0.30 / cache_write 3.75
  - Haiku 4.5: in 1.00 / out 5.00 / cache_read 0.10 / cache_write 1.25
- **Price table is config, not code** — this is the drift-resilience guarantee.
- Unknown model → cost 0 **and a warning log** (never silently miss).
- Cache-write TTL split (5m vs 1h, 1.25× vs 2×) is a known approximation in MVP;
  refine when the `cache_creation` sub-object is reliably present.

### 3.4 Budgets & alerting
- Config defines budgets: `global.daily`, optional `global.monthly`, and
  `per_agent.<id>.daily` (with a default).
- Thresholds: `warn_pct` (default 80) and `critical_pct` (default 100).
- After each event write, recompute *today's* spend for that agent and globally,
  compare to budget.
- **Hysteresis / de-dup:** alert at most once per (scope, threshold, day). Track
  last-alerted level per scope/day so it doesn't spam every request past 80%.
- **Day boundary:** "daily" resets at local midnight in a configurable `timezone`
  (default `America/New_York`).
- **Notifier interface** with Discord (webhook) and Telegram (bot) impls; config
  selects one or both. Examples default to Discord (James is mid Telegram→Discord
  migration). Alert payload: scope, period, spent (est.), budget, %, top
  contributing agents.
- **Enforcement is opt-in and OFF by default.** A `enforce: true` flag lets the
  gateway return 429 on over-budget requests (a watchdog "with teeth"). Default
  observe-only to respect fail-open and avoid surprising agents.

### 3.5 Config (single file, e.g. TOML via stdlib `tomllib`)
Contains: upstream URL, listen port, timezone, price table, budgets, thresholds,
enforcement flag, privacy/debug flags, notifier *selection*. **Secrets are NOT in
the file** — notifier tokens/webhooks via env vars; API keys never touch config.
Ship `config.example.toml`; `.gitignore` the live config.

### 3.6 CLI
- `watchdog serve` — run the gateway daemon.
- `watchdog status [--agent X]` — current day spend vs budget from SQLite.
- `watchdog report --since <date>` — aggregate report.

---

## 4. Tech stack
- **Python 3.11+** (James's stack; ideal for a small async proxy + SQLite + HTTP).
- Async gateway: **Starlette + uvicorn** with `httpx.AsyncClient.stream()` for
  SSE passthrough (alt: `aiohttp`). Minimal deps: `starlette`, `uvicorn`,
  `httpx`. Config via stdlib `tomllib`. SQLite via stdlib `sqlite3`.
- Notifiers via `httpx`.
- Packaging: `pyproject.toml`, console entry point `watchdog`, sample LaunchAgent
  plist (James already runs LaunchAgents).

---

## 5. What it can and cannot see (be honest in the README)
- **Sees:** every Messages API call routed through the gateway, any language/SDK
  — token usage + model.
- **Doesn't see:** Managed Agents internal model calls (run Anthropic-side);
  any traffic not pointed at the gateway; subscription-token agents have no
  dollar meter. Cost numbers are **estimates** until phase-2 reconciliation.

---

## 6. Testing strategy (testing is mandatory)
- **Unit:** cost computation incl. cache rates; budget threshold + hysteresis;
  day-boundary/timezone reset; usage extraction from canned non-streaming and
  streaming SSE payloads; notifier payload formatting (mocked HTTP).
- **Integration:** gateway against a mock upstream returning canned streaming +
  non-streaming responses — assert byte-fidelity passthrough (status/headers/
  body), event rows written, and **fail-open** (upstream error AND injected
  metering error → request still succeeds).
- **Dogfood:** point one real ClaudeClaw agent at the gateway.

---

## 7. Milestones
- **M0** — repo + this spec + config schema + `config.example.toml`.
- **M1** — gateway passthrough (non-streaming + streaming), fail-open, usage
  extraction → SQLite. Verify passthrough fidelity.
- **M2** — cost engine + config price table + `watchdog status`.
- **M3** — budgets + threshold/hysteresis + Discord notifier (+ Telegram).
- **M4** — LaunchAgent packaging, README, tests green. → **MVP ship.**
- **Phase 2** — Admin Cost/Usage API collector (reconciliation, org-level
  authoritative-dollar ceiling, price-drift alerts); dashboard view; optional
  enforcement hardening; rate-limit-headroom signal for subscription agents.

---

## 8. Open risks
- Gateway sits in the request path → reliability/latency risk. Mitigate:
  fail-open, upstream timeouts, no full-body buffering, health endpoint.
- Streaming usage-parse correctness → test against real SSE shapes.
- Managed Agents blind spot → documented; phase-2 Admin API covers it.
- Price-table drift → config + phase-2 reconciliation is the backstop.
- "Daily" correctness depends on timezone config.
- Per-agent dollars are estimates; only org/workspace dollars become
  authoritative in phase 2 (and only at workspace granularity).
