"""Command-line entry point: `tokenwarden serve | status | report`."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from tokenwarden.config import Config
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
        log.warning(
            "enforce=true is set, but request blocking is not implemented yet "
            "(alerts only) — over-budget requests are NOT blocked"
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
    args = parser.parse_args(argv)

    handlers = {"serve": _serve, "status": _status, "report": _report}
    return handlers[args.command](args)
