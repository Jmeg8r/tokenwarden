import asyncio

from tokenwarden.alerts import AlertManager
from tokenwarden.config import Budgets, Config


class _FakeStorage:
    """spend_today() returns canned totals; key None == global."""

    def __init__(self) -> None:
        self.totals: dict = {}

    def spend_today(self, tz, agent_id=None):
        return self.totals.get(agent_id, 0.0)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.alerts: list = []

    async def notify(self, alert):
        self.alerts.append(alert)


def _manager(storage, **budget_kwargs):
    config = Config(budgets=Budgets(**budget_kwargs))
    notifier = _RecordingNotifier()
    return AlertManager(config, storage, notifier), notifier


def test_warn_fires_once_per_day():
    storage = _FakeStorage()
    mgr, notifier = _manager(storage, default_agent_daily=10.0)
    storage.totals["forge"] = 8.0  # 80% -> warn
    asyncio.run(mgr.evaluate("forge", day="2026-06-16"))
    storage.totals["forge"] = 9.0  # still in warn band
    asyncio.run(mgr.evaluate("forge", day="2026-06-16"))
    assert [a.level for a in notifier.alerts] == ["warn"]


def test_escalates_to_critical_once():
    storage = _FakeStorage()
    mgr, notifier = _manager(storage, default_agent_daily=10.0)
    storage.totals["forge"] = 8.0
    asyncio.run(mgr.evaluate("forge", day="2026-06-16"))  # warn
    storage.totals["forge"] = 10.0
    asyncio.run(mgr.evaluate("forge", day="2026-06-16"))  # critical
    storage.totals["forge"] = 12.0
    asyncio.run(mgr.evaluate("forge", day="2026-06-16"))  # no repeat
    assert [a.level for a in notifier.alerts] == ["warn", "critical"]


def test_jump_straight_to_critical():
    storage = _FakeStorage()
    mgr, notifier = _manager(storage, default_agent_daily=10.0)
    storage.totals["forge"] = 25.0
    asyncio.run(mgr.evaluate("forge", day="2026-06-16"))
    assert [a.level for a in notifier.alerts] == ["critical"]


def test_new_day_resets_hysteresis():
    storage = _FakeStorage()
    mgr, notifier = _manager(storage, default_agent_daily=10.0)
    storage.totals["forge"] = 8.0
    asyncio.run(mgr.evaluate("forge", day="2026-06-16"))
    asyncio.run(mgr.evaluate("forge", day="2026-06-17"))
    assert len(notifier.alerts) == 2


def test_global_scope_alerts():
    storage = _FakeStorage()
    mgr, notifier = _manager(storage, global_daily=100.0)
    storage.totals[None] = 100.0  # global spend at 100%
    asyncio.run(mgr.evaluate("any-agent", day="2026-06-16"))
    assert len(notifier.alerts) == 1
    assert notifier.alerts[0].scope == "global" and notifier.alerts[0].level == "critical"


def test_no_budget_no_alert():
    storage = _FakeStorage()
    mgr, notifier = _manager(storage)  # no budgets configured
    storage.totals["forge"] = 9999.0
    asyncio.run(mgr.evaluate("forge", day="2026-06-16"))
    assert notifier.alerts == []
