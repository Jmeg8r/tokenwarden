from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tokenwarden.models import Usage
from tokenwarden.storage import Storage


def _usage():
    return Usage(model="claude-opus-4-8", input_tokens=100, output_tokens=50)


def test_record_is_idempotent_on_request_id(tmp_path):
    s = Storage(str(tmp_path / "t.db"))
    try:
        assert (
            s.record_event(
                ts="2999-01-01T00:00:00+00:00",
                agent_id="forge",
                usage=_usage(),
                cost_usd=1.5,
                request_id="req_1",
            )
            is True
        )
        # Same request_id again => ignored, not double-counted.
        assert (
            s.record_event(
                ts="2999-01-01T00:00:00+00:00",
                agent_id="forge",
                usage=_usage(),
                cost_usd=1.5,
                request_id="req_1",
            )
            is False
        )
    finally:
        s.close()


def test_spend_today_total_and_by_agent(tmp_path):
    s = Storage(str(tmp_path / "t.db"))
    try:
        tz = ZoneInfo("UTC")
        now = datetime.now(timezone.utc).isoformat()
        s.record_event(ts=now, agent_id="forge", usage=_usage(), cost_usd=2.0, request_id="a")
        s.record_event(ts=now, agent_id="scout", usage=_usage(), cost_usd=3.0, request_id="b")

        assert round(s.spend_today(tz), 4) == 5.0
        assert round(s.spend_today(tz, agent_id="forge"), 4) == 2.0

        by_agent = s.spend_by_agent_today(tz)
        assert by_agent == {"scout": 3.0, "forge": 2.0}
    finally:
        s.close()


def _hour_floor(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def test_hourly_spend_buckets_zero_fills_and_filters(tmp_path):
    s = Storage(str(tmp_path / "t.db"))
    try:
        tz = ZoneInfo("UTC")
        now = datetime.now(timezone.utc)
        h3 = _hour_floor(now - timedelta(hours=3))
        h2 = _hour_floor(now - timedelta(hours=2))
        h1 = _hour_floor(now - timedelta(hours=1))
        # Two events land in the same hour (h3) -> they must sum; one in h2.
        s.record_event(ts=(h3 + timedelta(minutes=5)).isoformat(), agent_id="forge",
                       usage=_usage(), cost_usd=1.0, request_id="a")
        s.record_event(ts=(h3 + timedelta(minutes=50)).isoformat(), agent_id="forge",
                       usage=_usage(), cost_usd=2.0, request_id="b")
        s.record_event(ts=(h2 + timedelta(minutes=10)).isoformat(), agent_id="scout",
                       usage=_usage(), cost_usd=4.0, request_id="c")

        series = s.hourly_spend(tz, lookback_days=2)
        d = dict(series)
        assert round(d[h3], 4) == 3.0        # summed within the hour
        assert round(d[h2], 4) == 4.0
        assert d.get(h1, 0.0) == 0.0         # zero-filled gap

        # Series is dense: sorted, evenly spaced one hour apart.
        times = [t for t, _ in series]
        assert times == sorted(times)
        assert all(times[i + 1] - times[i] == timedelta(hours=1) for i in range(len(times) - 1))

        # Agent filter isolates one agent's spend.
        f = dict(s.hourly_spend(tz, lookback_days=2, agent_id="forge"))
        assert round(f[h3], 4) == 3.0
        assert f.get(h2, 0.0) == 0.0
    finally:
        s.close()


def test_list_agents_distinct_and_sorted(tmp_path):
    s = Storage(str(tmp_path / "t.db"))
    try:
        tz = ZoneInfo("UTC")
        now = datetime.now(timezone.utc)
        recent = _hour_floor(now - timedelta(hours=1)) + timedelta(minutes=5)
        s.record_event(ts=recent.isoformat(), agent_id="scout", usage=_usage(),
                       cost_usd=1.0, request_id="a")
        s.record_event(ts=recent.isoformat(), agent_id="forge", usage=_usage(),
                       cost_usd=1.0, request_id="b")
        s.record_event(ts=recent.isoformat(), agent_id="forge", usage=_usage(),
                       cost_usd=1.0, request_id="c")
        assert s.list_agents(tz, lookback_days=2) == ["forge", "scout"]
    finally:
        s.close()
