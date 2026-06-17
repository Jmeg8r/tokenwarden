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

    def should_block(self, agent_id: str) -> Alert | None:
        """Pre-request enforcement check: is this agent OR the global pool already
        at/over the critical threshold for today? Returns the breaching Alert (so
        the caller can explain why), else None.

        Fail-open: any error returns None (allow). Note this sees spend *before*
        the current request — its cost isn't known until the response — so the
        request that tips you over still goes through; the next one is blocked.
        """
        try:
            budgets = self._config.budgets
            tz = self._config.tzinfo
            day = datetime.now(tz).strftime("%Y-%m-%d")
            agent_budget = budgets.daily_for(agent_id)
            if agent_budget:
                spent = self._storage.spend_today(tz, agent_id)
                if self._level_for(spent / agent_budget * 100.0) == _CRITICAL:
                    return Alert(
                        scope=f"agent:{agent_id}", level="critical", spent=spent,
                        budget=agent_budget, pct=spent / agent_budget * 100.0, day=day,
                    )
            if budgets.global_daily:
                spent = self._storage.spend_today(tz)
                if self._level_for(spent / budgets.global_daily * 100.0) == _CRITICAL:
                    return Alert(
                        scope="global", level="critical", spent=spent,
                        budget=budgets.global_daily, pct=spent / budgets.global_daily * 100.0, day=day,
                    )
            return None
        except Exception:  # noqa: BLE001 — enforcement must never hard-fail traffic
            log.exception("budget pre-check failed; allowing request (fail-open)")
            return None
