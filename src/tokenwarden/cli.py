"""Command-line entry point: `tokenwarden serve | status | report`."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from tokenwarden.config import FORECAST_BACKENDS, Config
from tokenwarden.storage import Storage

log = logging.getLogger("tokenwarden")


def _load_config(args: argparse.Namespace) -> Config:
    if args.config and Path(args.config).exists():
        return Config.load(args.config)
    log.warning("no config file at %r — using built-in defaults", args.config)
    return Config()


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from tokenwarden.gateway import create_app

    config = _load_config(args)
    storage = Storage(config.db_path)
    app = create_app(config, storage)
    log.info(
        "serving on %s:%d -> upstream %s (enforce=%s, db=%s)",
        config.host,
        config.port,
        config.upstream_url,
        config.enforce,
        config.db_path,
    )
    if config.enforce:
        log.info(
            "enforcement ON — requests are refused with HTTP 429 once an agent or "
            "the global pool is at/over the critical budget threshold"
        )
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    return 0


def _status(args: argparse.Namespace) -> int:
    config = _load_config(args)
    storage = Storage(config.db_path)
    try:
        by_agent = storage.spend_by_agent_today(config.tzinfo)
        total = sum(by_agent.values())
        print(f"Estimated spend today ({config.timezone}):")
        if not by_agent:
            print("  (no events recorded yet)")
        for agent, spent in by_agent.items():
            budget = config.budgets.daily_for(agent)
            suffix = f"  / ${budget:,.2f} budget" if budget is not None else ""
            print(f"  {agent:<24} ${spent:,.4f}{suffix}")
        print(f"  {'TOTAL':<24} ${total:,.4f}")
    finally:
        storage.close()
    return 0


def _report(args: argparse.Namespace) -> int:
    print("report: not implemented yet (milestone M2/M3)")
    return 0


def _forecast_scope(storage, forecaster, config, scope, agent_id, horizon, day):
    """Forecast one scope (global or a single agent), print its line, and return
    any (projected-overrun, anomaly) alerts raised."""
    from tokenwarden.forecast import (
        evaluate_anomaly,
        evaluate_overrun,
        project_end_of_day,
    )
    from tokenwarden.notifiers import format_alert

    tz = config.tzinfo
    fc_cfg = config.forecasting
    values = [v for _, v in storage.hourly_spend(tz, fc_cfg.lookback_days, agent_id)]
    observed_today = storage.spend_today(tz, agent_id)
    remaining = forecaster.forecast(values, horizon)
    proj = project_end_of_day(observed_today, remaining)
    budget = config.budgets.global_daily if agent_id is None else config.budgets.daily_for(agent_id)

    label = "global" if agent_id is None else agent_id
    budget_str = f" / ${budget:,.2f} budget" if budget else ""
    conf = "  [low confidence: sparse history]" if remaining.low_confidence else ""
    print(
        f"  {label:<24} projected ${proj.point:,.4f} "
        f"(band ${proj.lower:,.2f}–${proj.upper:,.2f}){budget_str}{conf}"
    )

    alerts = []
    over = evaluate_overrun(scope, day, proj, budget, config.warn_pct, config.critical_pct)
    if over:
        print(f"    ! {format_alert(over)}")
        alerts.append(over)
    # Anomaly on the last *complete* hour — values[-1] is the current partial hour.
    if len(values) >= 3:
        anomaly = evaluate_anomaly(
            scope, day, values[:-2], values[-2], forecaster, fc_cfg.anomaly_factor
        )
        if anomaly:
            print(f"    ! {format_alert(anomaly)}")
            alerts.append(anomaly)
    return alerts


def _forecast(args: argparse.Namespace) -> int:
    from tokenwarden.forecast import build_forecaster

    config = _load_config(args)
    if args.backend:
        config.forecasting.backend = args.backend
    storage = Storage(config.db_path)
    try:
        forecaster = build_forecaster(config)
        tz = config.tzinfo
        now_local = datetime.now(tz)
        day = now_local.strftime("%Y-%m-%d")
        # Whole hours remaining until local midnight (the current hour is already
        # counted in observed_today, so we forecast only the hours after it).
        horizon = 23 - now_local.hour
        agents = [args.agent] if args.agent else storage.list_agents(tz, config.forecasting.lookback_days)

        print(f"End-of-day spend forecast for {day} ({config.timezone}), backend={config.forecasting.backend}:")
        alerts = _forecast_scope(storage, forecaster, config, "global", None, horizon, day)
        for agent in agents:
            alerts += _forecast_scope(storage, forecaster, config, f"agent:{agent}", agent, horizon, day)
        if not agents:
            print("  (no agents with recorded spend in the lookback window)")

        if args.notify and alerts:
            from tokenwarden.notifiers import build_notifier

            notifier = build_notifier(config)

            async def _send() -> None:
                # One flaky channel must not drop the rest of the run's alerts.
                for alert in alerts:
                    try:
                        await notifier.notify(alert)
                    except Exception:  # noqa: BLE001
                        log.exception("failed to send alert via notifier: %s", alert)

            asyncio.run(_send())
            print(f"  sent {len(alerts)} alert(s) via {config.notifier_channels or 'no channels'}")
    finally:
        storage.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(prog="tokenwarden", description="Claude API spend watchdog.")
    # --config is shared by every subcommand and accepted *after* it
    # (e.g. `tokenwarden serve --config foo.toml`), which is what users type.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", default="config.toml", help="path to config TOML (default: config.toml)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", parents=[common], help="run the metering gateway")
    sub.add_parser("status", parents=[common], help="show today's estimated spend by agent")
    sub.add_parser("report", parents=[common], help="aggregate report (not yet implemented)")
    fc = sub.add_parser(
        "forecast", parents=[common], help="project end-of-day spend and flag likely overruns"
    )
    fc.add_argument("--agent", help="forecast only this agent (default: all agents seen)")
    fc.add_argument(
        "--backend", choices=sorted(FORECAST_BACKENDS), help="override forecasting.backend"
    )
    fc.add_argument(
        "--notify", action="store_true", help="send any alerts via the configured notifier channels"
    )
    args = parser.parse_args(argv)

    handlers = {"serve": _serve, "status": _status, "report": _report, "forecast": _forecast}
    return handlers[args.command](args)
