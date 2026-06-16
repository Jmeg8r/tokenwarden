"""Daily budget evaluation with per-day, per-scope alert hysteresis."""

from __future__ import annotations

import logging
from datetime import datetime

from tokenwarden.config import Config
from tokenwarden.models import Alert
from tokenwarden.notifiers import Notifier
from tokenwarden.storage import Storage

log = logging.getLogger("tokenwarden.alerts")

_NONE, _WARN, _CRITICAL = 0, 1, 2


class AlertManager:
    """Evaluates per-agent and global daily budgets after each metered event and
    fires at most one alert per (scope, threshold, day) — so crossing 80% pings
    once, not on every request after.

    Hysteresis state is in-memory; a restart may re-alert once per active scope
    (acceptable for an MVP — persisting it is a later refinement).
    """

    def __init__(self, config: Config, storage: Storage, notifier: Notifier) -> None:
        self._config = config
        self._storage = storage
        self._notifier = notifier
        self._last_level: dict[tuple[str, str], int] = {}

    def _level_for(self, pct: float) -> int:
        if pct >= self._config.critical_pct:
            return _CRITICAL
        if pct >= self._config.warn_pct:
            return _WARN
        return _NONE

    async def evaluate(self, agent_id: str, day: str | None = None) -> None:
        budgets = self._config.budgets
        agent_budget = budgets.daily_for(agent_id)
        if not agent_budget and not budgets.global_daily:
            return  # nothing budgeted — skip the spend query entirely
        tz = self._config.tzinfo
        day = day or datetime.now(tz).strftime("%Y-%m-%d")
        if agent_budget:
            await self._check(
                f"agent:{agent_id}", day, self._storage.spend_today(tz, agent_id), agent_budget
            )
        if budgets.global_daily:
            await self._check("global", day, self._storage.spend_today(tz), budgets.global_daily)

    async def _check(self, scope: str, day: str, spent: float, budget: float) -> None:
        if budget <= 0:
            return
        pct = spent / budget * 100.0
        level = self._level_for(pct)
        key = (scope, day)
        # Only fire when the threshold level rises beyond what we've already sent today.
        if level <= self._last_level.get(key, _NONE):
            return
        self._last_level[key] = level
        alert = Alert(
            scope=scope,
            level="critical" if level == _CRITICAL else "warn",
            spent=spent,
            budget=budget,
            pct=pct,
            day=day,
        )
        log.info("budget alert: %s at %.0f%% (%s)", scope, pct, alert.level)
        await self._notifier.notify(alert)
