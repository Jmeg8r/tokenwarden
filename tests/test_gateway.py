"""Integration tests for the proxy: passthrough fidelity, metering, fail-open.

The upstream Anthropic API is replaced with a tiny in-process ASGI app reached
via httpx's ASGITransport. That exercises the real streaming code path (a true
async byte stream), unlike a MockTransport with eager `content=` bytes. The
gateway app itself is driven through Starlette's TestClient.
"""

import json

import httpx
from starlette.applications import Starlette
from starlette.responses import Response as StarletteResponse
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tokenwarden.config import Config
from tokenwarden.gateway import create_app
from tokenwarden.storage import Storage

JSON_BODY = json.dumps(
    {
        "type": "message",
        "model": "claude-opus-4-8",
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": 100, "output_tokens": 25},
    }
).encode()


def _sse_body() -> bytes:
    def ev(e: dict) -> bytes:
        return b"event: " + e["type"].encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n"

    return (
        ev(
            {
                "type": "message_start",
                "message": {"model": "claude-sonnet-4-6", "usage": {"input_tokens": 40, "output_tokens": 1}},
            }
        )
        + ev({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}})
        + ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}})
    )


def _json_upstream(body: bytes, *, request_id: str | None = None, capture: dict | None = None) -> Starlette:
    async def endpoint(request):
        if capture is not None:
            capture["path"] = request.url.path
            capture["api_key"] = request.headers.get("x-api-key")
            capture["watchdog_header_forwarded"] = "x-watchdog-agent" in request.headers
            capture["accept_encoding"] = request.headers.get("accept-encoding")
        headers = {"request-id": request_id} if request_id else {}
        return StarletteResponse(body, status_code=200, media_type="application/json", headers=headers)

    return Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST"])])


def _sse_upstream(body: bytes, *, request_id: str = "req_s") -> Starlette:
    async def endpoint(request):
        async def gen():
            # Yield in small slices to exercise the gateway's incremental parsing.
            for i in range(0, len(body), 16):
                yield body[i : i + 16]

        return StreamingResponse(
            gen(), status_code=200, media_type="text/event-stream", headers={"request-id": request_id}
        )

    return Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST"])])


def _gateway(tmp_path, upstream_app=None, transport=None):
    config = Config(db_path=str(tmp_path / "g.db"), upstream_url="http://upstream.test")
    storage = Storage(config.db_path)
    if transport is None:
        transport = httpx.ASGITransport(app=upstream_app)
    upstream = httpx.AsyncClient(base_url="http://upstream.test", transport=transport)
    return create_app(config, storage, upstream_client=upstream), storage, config


def test_passthrough_json_and_meters(tmp_path):
    captured: dict = {}
    app, storage, config = _gateway(tmp_path, _json_upstream(JSON_BODY, request_id="req_xyz", capture=captured))
    try:
        with TestClient(app) as client:
            r = client.post(
                "/v1/messages",
                content=b'{"model":"x"}',
                headers={"x-api-key": "sk-ant-api03-xxx", "x-watchdog-agent": "forge"},
            )
        assert r.status_code == 200
        assert r.content == JSON_BODY  # byte-faithful passthrough
        assert captured["path"] == "/v1/messages"
        assert captured["api_key"] == "sk-ant-api03-xxx"  # auth forwarded
        assert captured["watchdog_header_forwarded"] is False  # internal header stripped
        assert captured["accept_encoding"] == "identity"  # forced, so httpx can't gzip the upstream

        expected = 100 / 1e6 * 5 + 25 / 1e6 * 25
        assert round(storage.spend_today(config.tzinfo, agent_id="forge"), 6) == round(expected, 6)
    finally:
        storage.close()


def test_passthrough_streaming_and_meters(tmp_path):
    body = _sse_body()
    app, storage, config = _gateway(tmp_path, _sse_upstream(body))
    try:
        with TestClient(app) as client:
            r = client.post("/v1/messages", headers={"x-watchdog-agent": "scout"})
        assert r.status_code == 200
        assert r.content == body  # streamed bytes reassemble exactly

        expected = 40 / 1e6 * 3 + 12 / 1e6 * 15  # sonnet 4.6 rates, in=40 out=12
        assert round(storage.spend_today(config.tzinfo, agent_id="scout"), 6) == round(expected, 6)
    finally:
        storage.close()


def test_fail_open_when_metering_raises(tmp_path, monkeypatch):
    app, storage, config = _gateway(tmp_path, _json_upstream(JSON_BODY))

    def boom(*args, **kwargs):
        raise RuntimeError("storage is on fire")

    monkeypatch.setattr(storage, "record_event", boom)
    try:
        with TestClient(app) as client:
            r = client.post("/v1/messages", headers={"x-watchdog-agent": "forge"})
        # The request must succeed unaffected even though metering blew up.
        assert r.status_code == 200
        assert r.content == JSON_BODY
    finally:
        storage.close()


def test_upstream_error_returns_502(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down")

    app, storage, _ = _gateway(tmp_path, transport=httpx.MockTransport(handler))
    try:
        with TestClient(app) as client:
            r = client.post("/v1/messages")
        assert r.status_code == 502
    finally:
        storage.close()


def test_unattributed_when_no_header(tmp_path):
    app, storage, config = _gateway(tmp_path, _json_upstream(JSON_BODY))
    try:
        with TestClient(app) as client:
            r = client.post("/v1/messages")
        assert r.status_code == 200
        # Falls back to the "unattributed" bucket rather than dropping the spend.
        assert storage.spend_today(config.tzinfo, agent_id="unattributed") > 0
    finally:
        storage.close()


class _RecordingNotifier:
    def __init__(self) -> None:
        self.alerts: list = []

    async def notify(self, alert) -> None:
        self.alerts.append(alert)


def test_request_over_budget_triggers_alert(tmp_path):
    from tokenwarden.alerts import AlertManager
    from tokenwarden.config import Budgets

    recording = _RecordingNotifier()
    # JSON_BODY costs ~$0.001125 on Opus 4.8; a tiny budget guarantees a crossing.
    config = Config(
        db_path=str(tmp_path / "g.db"),
        upstream_url="http://upstream.test",
        budgets=Budgets(default_agent_daily=0.0005),
    )
    storage = Storage(config.db_path)
    upstream = httpx.AsyncClient(
        base_url="http://upstream.test", transport=httpx.ASGITransport(app=_json_upstream(JSON_BODY))
    )
    alerts = AlertManager(config, storage, recording)
    app = create_app(config, storage, upstream_client=upstream, alert_manager=alerts)
    try:
        with TestClient(app) as client:
            r = client.post("/v1/messages", headers={"x-watchdog-agent": "forge"})
        assert r.status_code == 200
        assert recording.alerts, "expected a budget alert to fire"
        assert recording.alerts[0].level == "critical"
    finally:
        storage.close()


def test_enforce_blocks_over_budget_without_contacting_upstream(tmp_path):
    from datetime import datetime, timezone

    from tokenwarden.alerts import AlertManager
    from tokenwarden.config import Budgets
    from tokenwarden.models import Usage

    upstream_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls["n"] += 1
        return httpx.Response(200, content=JSON_BODY, headers={"content-type": "application/json"})

    config = Config(
        db_path=str(tmp_path / "g.db"),
        upstream_url="http://upstream.test",
        enforce=True,
        budgets=Budgets(default_agent_daily=1.0),
    )
    storage = Storage(config.db_path)
    # Pre-seed: agent already over its $1 budget today.
    storage.record_event(
        ts=datetime.now(timezone.utc).isoformat(),
        agent_id="forge",
        usage=Usage(model="claude-opus-4-8"),
        cost_usd=2.0,
        request_id="seed",
    )
    upstream = httpx.AsyncClient(base_url="http://upstream.test", transport=httpx.MockTransport(handler))
    alerts = AlertManager(config, storage, _RecordingNotifier())
    app = create_app(config, storage, upstream_client=upstream, alert_manager=alerts)
    try:
        with TestClient(app) as client:
            r = client.post("/v1/messages", headers={"x-watchdog-agent": "forge"})
        assert r.status_code == 429
        assert upstream_calls["n"] == 0  # refused before contacting Anthropic
        assert "budget" in r.text.lower()
    finally:
        storage.close()


def test_no_enforce_lets_over_budget_through(tmp_path):
    from datetime import datetime, timezone

    from tokenwarden.config import Budgets
    from tokenwarden.models import Usage

    config = Config(
        db_path=str(tmp_path / "g.db"),
        upstream_url="http://upstream.test",
        enforce=False,
        budgets=Budgets(default_agent_daily=1.0),
    )
    storage = Storage(config.db_path)
    storage.record_event(
        ts=datetime.now(timezone.utc).isoformat(),
        agent_id="forge",
        usage=Usage(model="claude-opus-4-8"),
        cost_usd=2.0,
        request_id="seed",
    )
    upstream = httpx.AsyncClient(
        base_url="http://upstream.test", transport=httpx.ASGITransport(app=_json_upstream(JSON_BODY))
    )
    app = create_app(config, storage, upstream_client=upstream)
    try:
        with TestClient(app) as client:
            r = client.post("/v1/messages", headers={"x-watchdog-agent": "forge"})
        assert r.status_code == 200  # observe-only: not blocked
        assert r.content == JSON_BODY
    finally:
        storage.close()
