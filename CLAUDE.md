# CLAUDE.md — tokenwarden

Repo-specific notes for Claude Code. General workflow/conventions live in the global
`~/.claude/CLAUDE.md`; this file only captures what's specific to this project.

## What this is
A **local gateway that meters Claude API spend per agent** and alerts on daily budgets. Agents
point `ANTHROPIC_BASE_URL` at it (default `http://127.0.0.1:8788`) and tag themselves with an
`X-Watchdog-Agent` header; tokenwarden forwards to `api.anthropic.com`, meters token usage, prices
it, and records spend to a local SQLite DB. See `SPEC.md` for the full design.

## Where things are
- `src/tokenwarden/gateway.py` — the Starlette/uvicorn reverse-proxy (must stay **fail-open**:
  metering must never break or delay the agent's call).
- `usage.py` + `pricing.py` — token accounting and the configurable price table.
- `storage.py` — SQLite (`tokenwarden.db`); `alerts.py` + `notifiers.py` — daily-budget alerts.
- `cli.py` — `tokenwarden` entrypoint (`serve`, `status`, …). Config in `config.toml`
  (`config.example.toml` is the template).

## Run / verify
```bash
make install     # venv (Python 3.13) + dev deps
make serve       # run the metering gateway
make status      # today's estimated spend by agent
make test        # pytest
make smoke       # live Part-A dogfood (needs ANTHROPIC_API_KEY)
```

## Conventions / gotchas
- **Never store or log secrets or prompt/response bodies** — tokenwarden meters metadata
  (tokens, agent, model, cost), not content. The API key is a passthrough; it must not be
  persisted.
- The proxy is **fail-open**: if metering fails, the upstream call still goes through.
- Pricing is source-of-truth in `pricing.py`/config — when Anthropic prices change, update the
  table there rather than hardcoding numbers elsewhere.
- `tokenwarden.db` and `config.toml` are local state — keep real config/secrets out of git.
- Branch/PR per the global workflow; never push to `main` directly.

## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues in `Jmeg8r/tokenwarden`, managed via the
`gh` CLI. External PRs are not a triage surface (issues only). See
`docs/agents/issue-tracker.md`.

### Triage labels

Canonical five-role vocabulary. `wontfix` already exists in the repo; the other
four (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`) are
created on first `/triage` use. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root, created lazily
by `/domain-modeling`. See `docs/agents/domain.md`.
