---
name: price-sync
description: Add or update a model's pricing in tokenwarden, end to end and from an authoritative source only — DEFAULT_PRICES in config.py, config.example.toml, tests, and verification of the unknown-model warning path. Use when Anthropic ships a new model or changes prices, when the user says "add pricing for <model>", "update the price table", or when logs show the "no price for model" warning.
---

# price-sync

Pricing is this tool's ground truth: a wrong number silently corrupts every spend figure,
budget alert, and forecast downstream. This skill exists to make price changes atomic
(all locations at once) and sourced (never guessed).

## Step 0 — Get an authoritative price. HARD STOP without one.

Accepted sources, in order of preference:
1. Anthropic's published pricing (fetch https://docs.anthropic.com/en/docs/about-claude/pricing
   or https://www.anthropic.com/pricing — confirm the page actually lists the model and
   per-MTok USD rates for input and output).
2. The maintainer stating the rates explicitly.

If neither is available: **stop**. Do not extrapolate from neighboring models, do not
pattern-match tiers. Report that the model stays unpriced and that this is safe by design —
`pricing.py::cost_usd` counts unknown models at $0 and logs
`"no price for model %r — counting $0; update the price table"` so the gap is visible,
not silent.

Record the source (URL or "maintainer, <date>") — it goes in the commit message.

## Step 1 — Update the two price locations (and only these two)

1. **`src/tokenwarden/config.py` → `DEFAULT_PRICES`.** Use the `_price(input, output)`
   helper, which derives cache rates as read = 0.1× input and 5-minute write = 1.25×
   input. Only spell out an explicit `Price(...)` with all four fields if the published
   cache rates differ from those multipliers.
2. **`config.example.toml` → `[prices.<model-id>]`.** Keep it consistent with the shipped
   defaults and the existing entries' style (input/output only when cache rates are
   derived).

Model IDs are the API model strings (e.g. `claude-opus-4-8`), not marketing names. For a
price *change* to an existing model, update both locations; for a *new* model, add to both.

Do NOT touch `pricing.py` (it reads the table; it holds no numbers), CLI output, README
prose, or tests' production paths with literals. If the README or `SPEC.md` names specific
rates in prose, update those sentences too — but numbers used in computation live only in
the two locations above.

## Step 2 — Tests

- Extend `tests/test_pricing.py` (or `tests/test_config.py` for load/derivation behavior)
  to cover the new/changed entry: cost computed from the new rates, and cache-rate
  derivation if you relied on `_price`.
- Confirm the unknown-model path still warns: `test_pricing.py` has the caplog-based test;
  it must still pass unmodified.
- Run the suite:
  ```bash
  make test
  ```

## Step 3 — Sanity check against live data (when available)

If `tokenwarden.db` has traffic for the affected model, spot-check that recomputed costs
look right:

```bash
make status
```

Past events keep their stored `cost_usd` (the log is append-only history, priced at
record time — do not rewrite old rows); only new events use the new table. Say this in
the PR if a price changed, so nobody expects historical rows to shift.

## Step 4 — Ship

Commit as one atomic conventional commit, citing the source:

```text
feat: price table for <model-id> (in $X/MTok, out $Y/MTok)

Source: <URL or "maintainer, YYYY-MM-DD">
```

Then run `/tokenwarden-ship` (branch → PR → CodeRabbit loop). Update `STATE.md`'s
resume pointer via `/session-state`.
