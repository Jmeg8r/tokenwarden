from datetime import datetime, timedelta, timezone

from tokenwarden.cli import main
from tokenwarden.models import Usage
from tokenwarden.storage import Storage


def test_status_accepts_config_after_subcommand(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[gateway]\ndb_path = "{tmp_path / "x.db"}"\n')
    # Regression: `--config` must be accepted *after* the subcommand
    # (`tokenwarden status --config foo.toml`), not only before it.
    assert main(["status", "--config", str(cfg)]) == 0


def test_forecast_naive_end_to_end(tmp_path, capsys):
    db = tmp_path / "f.db"
    s = Storage(str(db))
    try:
        now = datetime.now(timezone.utc)
        usage = Usage(model="claude-opus-4-8", input_tokens=100, output_tokens=50)
        # ~3 days of hourly spend for one agent → a real series to forecast.
        for i in range(72):
            ts = (now - timedelta(hours=i)).isoformat()
            s.record_event(ts=ts, agent_id="forge", usage=usage, cost_usd=1.0, request_id=f"r{i}")
    finally:
        s.close()

    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[gateway]\ndb_path = "{db}"\ntimezone = "UTC"\n'
        "[budgets]\ndefault_agent_daily = 5.0\n"
        '[forecasting]\nbackend = "naive"\nlookback_days = 7\n'
    )
    assert main(["forecast", "--config", str(cfg), "--backend", "naive"]) == 0
    out = capsys.readouterr().out
    assert "End-of-day spend forecast" in out
    assert "forge" in out
    assert "projected" in out


def _seed_overrunning_db(db):
    s = Storage(str(db))
    try:
        now = datetime.now(timezone.utc)
        usage = Usage(model="claude-opus-4-8", input_tokens=100, output_tokens=50)
        for i in range(72):
            ts = (now - timedelta(hours=i)).isoformat()
            s.record_event(ts=ts, agent_id="forge", usage=usage, cost_usd=1.0, request_id=f"r{i}")
    finally:
        s.close()


def test_forecast_notify_survives_a_failing_channel(tmp_path, capsys, monkeypatch):
    """A notifier that raises must not abort the run — remaining alerts still
    attempt delivery, the summary reports the failure, and the command exits 0."""
    db = tmp_path / "f.db"
    _seed_overrunning_db(db)
    cfg = tmp_path / "c.toml"
    # Both a global and an agent budget are overrun, so the run carries >=2 alerts.
    cfg.write_text(
        f'[gateway]\ndb_path = "{db}"\ntimezone = "UTC"\n'
        "[budgets]\ndefault_agent_daily = 5.0\nglobal_daily = 5.0\n"
        '[forecasting]\nbackend = "naive"\nlookback_days = 7\n'
    )

    attempts = []

    class _FlakyNotifier:
        async def notify(self, alert):
            attempts.append(alert)
            if len(attempts) == 1:
                raise RuntimeError("channel down")

    monkeypatch.setattr("tokenwarden.notifiers.build_notifier", lambda config: _FlakyNotifier())
    assert main(["forecast", "--config", str(cfg), "--notify"]) == 0
    out = capsys.readouterr().out
    # The first alert failed, yet every remaining alert was still attempted...
    assert len(attempts) >= 2
    # ...and the summary reports what was actually delivered, not what was queued.
    assert f"sent {len(attempts) - 1}/{len(attempts)} alert(s)" in out
    assert "1 failed" in out
