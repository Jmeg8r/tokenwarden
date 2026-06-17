from datetime import datetime, timezone
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
