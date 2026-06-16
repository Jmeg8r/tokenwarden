# tokenwarden

A lightweight local gateway that meters **Claude API spend per agent**, logs
every billing event to SQLite, and (next milestone) alerts when daily spend
approaches a budget you set.

Point your agents at it with one environment variable. No SDK changes, no admin
key, works with any language.

> Why this exists: Anthropic's billing for programmatic / Agent-SDK usage is in
> flux (a planned 2026-06-15 split was paused on launch day — the third reversal
> in the area since January). tokenwarden defends against surprise charges, and
> it treats the price table as **config** so it doesn't break the next time rates
> change. See [SPEC.md](SPEC.md) for the full design.

## How it works

```
agent ──(ANTHROPIC_BASE_URL=http://127.0.0.1:8787)──▶ tokenwarden ──▶ api.anthropic.com
                                                          │
                                                  reads usage, writes
                                                  SQLite, (soon) alerts
```

- **Transparent reverse proxy.** Forwards every request to Anthropic untouched
  and streams the response straight back. It is **fail-open**: if metering ever
  errors, your agent still gets its response.
- **Per-agent attribution.** Each agent identifies itself with an
  `X-Watchdog-Agent: <name>` header. That label is whatever you choose.
- **Estimated cost** from a configurable price table (defaults ship for current
  models). Stored dollars are estimates until Cost-API reconciliation (phase 2).
- **No secrets stored.** Your API key is only forwarded, never persisted. Prompt
  bodies are not logged.

## Quickstart

```bash
pip install -e ".[dev]"
cp config.example.toml config.toml      # edit budgets/timezone as needed
tokenwarden serve                        # starts the gateway on 127.0.0.1:8787

# point an agent at it
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
# and have it send a header identifying itself, e.g. X-Watchdog-Agent: forge

tokenwarden status                       # today's estimated spend by agent
```

## Status

- **M0 — config** ✅ TOML schema, validation, configurable price table.
- **M1 — gateway** ✅ passthrough (JSON + streaming SSE), fail-open, usage
  extraction → SQLite, cost estimation, `serve` / `status` CLI.
- **M2** cost-engine polish + richer `status`/`report`.
- **M3** budgets + threshold alerts (Discord / Telegram).
- **M4** packaging (LaunchAgent), docs, MVP ship.
- **Phase 2** Admin Cost/Usage API reconciliation + drift detection.

## What it can't see (yet)

Managed Agents' internal model calls (the loop runs Anthropic-side) and
subscription-token agents (no per-token dollars). Both are covered by the
phase-2 Admin-API collector. See [SPEC.md](SPEC.md).

## License

MIT
