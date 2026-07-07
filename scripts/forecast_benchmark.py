#!/usr/bin/env python3
"""Backtest the spend forecasters against tokenwarden's own history.

For each complete past day in the DB, we pretend it's `--cutoff` o'clock on that
day: feed the forecaster all history up to that hour, project the end-of-day
total, and compare to what actually happened. Reports MAE / MAPE per backend so
you can see whether zero-shot TimesFM beats the stdlib naive baseline on *your*
spend — the As-The-Geek-Learns benchmark.

Usage:
    python scripts/forecast_benchmark.py --config config.toml [--cutoff 12] [--agent forge]

TimesFM is only benchmarked if the optional extra is installed
(`pip install "tokenwarden[forecast]"`); otherwise just the naive baseline runs.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime

from tokenwarden.config import Config
from tokenwarden.forecast import NaiveForecaster, project_end_of_day


def _load_series(db_path: str, tz, agent_id: str | None):
    """Return {local_date: [24 hourly spend floats]} from the event log."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    query = "SELECT ts, cost_usd FROM events"
    params: list = []
    if agent_id is not None:
        query += " WHERE agent_id = ?"
        params.append(agent_id)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    days: dict = {}
    for r in rows:
        local = datetime.fromisoformat(r["ts"]).astimezone(tz)
        days.setdefault(local.date(), [0.0] * 24)[local.hour] += float(r["cost_usd"])
    return days


def _flatten(days) -> tuple[list, list[float]]:
    """Ordered (date, hour) slots and a dense, zero-filled hourly value series."""
    slots, values = [], []
    for day in sorted(days):
        for hour in range(24):
            slots.append((day, hour))
            values.append(days[day][hour])
    return slots, values


def _backends(cfg: Config):
    forecasters = {"naive": NaiveForecaster(quantile=cfg.forecasting.quantile,
                                            min_history_hours=cfg.forecasting.min_history_hours)}
    try:
        from tokenwarden.forecast import TimesFMForecaster

        forecasters["timesfm"] = TimesFMForecaster(
            quantile=cfg.forecasting.quantile,
            checkpoint=cfg.forecasting.checkpoint,
            min_history_hours=cfg.forecasting.min_history_hours,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"(timesfm backend not benchmarked: {exc})")
    return forecasters


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--cutoff", type=int, default=12, help="hour-of-day to forecast from (0-23)")
    ap.add_argument("--agent", help="benchmark one agent (default: global total)")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    tz = cfg.tzinfo
    days = _load_series(cfg.db_path, tz, args.agent)
    slots, values = _flatten(days)
    slot_index = {s: i for i, s in enumerate(slots)}

    # Score every complete past day (skip the first — it has no prior history, and
    # skip the most recent, which may still be in progress).
    scored_days = sorted(days)[1:-1]
    if not scored_days:
        print("not enough history to backtest (need ≥3 days). Let the gateway run longer.")
        return 0

    forecasters = _backends(cfg)
    errors: dict[str, list[float]] = {name: [] for name in forecasters}
    ape: dict[str, list[float]] = {name: [] for name in forecasters}

    H = args.cutoff
    scope = args.agent or "global"
    print(f"Backtest: forecast end-of-day from {H:02d}:00, scope={scope}, {len(scored_days)} days\n")
    for day in scored_days:
        cut = slot_index[(day, H)]
        history = values[:cut]
        observed_by_H = sum(days[day][:H])
        actual = sum(days[day])
        horizon = 24 - H
        row = f"  {day}  actual ${actual:8.4f}"
        for name, fc in forecasters.items():
            forecast = fc.forecast(history, horizon)
            projected = project_end_of_day(observed_by_H, forecast).point
            err = projected - actual
            errors[name].append(abs(err))
            if actual > 0:
                ape[name].append(abs(err) / actual)
            row += f"   {name}=${projected:8.4f} (err ${err:+.4f})"
        print(row)

    print("\nSummary:")
    for name in forecasters:
        mae = sum(errors[name]) / len(errors[name])
        mape = (sum(ape[name]) / len(ape[name]) * 100.0) if ape[name] else float("nan")
        print(f"  {name:<8} MAE ${mae:.4f}   MAPE {mape:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
