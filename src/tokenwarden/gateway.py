"""The metering gateway: a transparent reverse proxy in front of the Claude API.

Design invariants:
- Byte-faithful passthrough: the agent must receive exactly what Anthropic sent.
- FAIL-OPEN: any failure in metering/cost/storage must NOT affect the proxied
  request. Metering is a best-effort side channel.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from tokenwarden.alerts import AlertManager
from tokenwarden.config import Config
from tokenwarden.models import Usage
from tokenwarden.notifiers import build_notifier
from tokenwarden.pricing import cost_usd
from tokenwarden.storage import Storage
from tokenwarden.usage import SSEUsageAccumulator, parse_json_usage

log = logging.getLogger("tokenwarden.gateway")

DEFAULT_AGENT = "unattributed"

# Cap the in-memory buffer for non-streaming bodies so a pathological response
# can't exhaust memory; beyond this we skip metering (fail-open).
MAX_JSON_BUFFER = 8 * 1024 * 1024

# Long-running Anthropic requests can stream for minutes — don't time them out.
UPSTREAM_TIMEOUT_SECONDS = 600.0

# Hop-by-hop headers, plus ones we deliberately rewrite.
# `accept-encoding` is dropped so upstream replies with identity encoding: that
# keeps passthrough byte-faithful AND lets the meter read the body (a gzipped
# body would otherwise be opaque to usage extraction).
_DROP_REQUEST_HEADERS = {
    "host",
    "accept-encoding",
    "connection",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}
# We re-stream the body, so length/encoding framing must be recomputed downstream.
_DROP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-encoding",
    "content-length",
    "trailer",
    "upgrade",
}


class _Meter:
    """Best-effort usage capture for one response. Every method is guarded so
    metering can never raise into the proxy path (fail-open)."""

    def __init__(self, content_type: str) -> None:
        self._sse = (
            SSEUsageAccumulator() if content_type.startswith("text/event-stream") else None
        )
        self._buf = bytearray()
        self._broken = False

    def feed(self, chunk: bytes) -> None:
        if self._broken:
            return
        try:
            if self._sse is not None:
                self._sse.feed(chunk)
            elif len(self._buf) + len(chunk) > MAX_JSON_BUFFER:
                self._broken = True
                self._buf = bytearray()
            else:
                self._buf += chunk
        except Exception:  # noqa: BLE001 — metering must not break proxying
            log.exception("meter.feed failed; disabling metering for this response")
            self._broken = True

    def result(self) -> Usage | None:
        if self._broken:
            return None
        try:
            if self._sse is not None:
                return self._sse.result()
            return parse_json_usage(bytes(self._buf))
        except Exception:  # noqa: BLE001
            log.exception("meter.result failed")
            return None


async def _meter_and_alert(
    storage: Storage,
    config: Config,
    alerts: AlertManager,
    agent_id: str,
    request_id: str | None,
    meter: _Meter,
) -> None:
    """Persist one event and evaluate budgets. Fully guarded — never raises
    (fail-open): metering or alerting failures must not affect the proxied
    request, whose body has already been delivered by the time this runs."""
    try:
        usage = meter.result()
        if usage is None or usage.is_empty:
            return
        cost = cost_usd(usage, config.prices)
        ts = datetime.now(timezone.utc).isoformat()
        inserted = storage.record_event(
            ts=ts, agent_id=agent_id, usage=usage, cost_usd=cost, request_id=request_id
        )
        if inserted:
            log.info(
                "metered agent=%s model=%s in=%d out=%d est=$%.5f",
                agent_id,
                usage.model,
                usage.input_tokens,
                usage.output_tokens,
                cost,
            )
            await alerts.evaluate(agent_id)
    except Exception:  # noqa: BLE001 — never let metering/alerting break the request
        log.exception("metering/alerting failed (request continued normally)")


async def _proxy(request: Request) -> Response:
    config: Config = request.app.state.config
    storage: Storage = request.app.state.storage
    client: httpx.AsyncClient = request.app.state.upstream
    alerts: AlertManager = request.app.state.alerts

    agent_id = request.headers.get(config.agent_header) or DEFAULT_AGENT
    body = await request.body()
    fwd_headers = [
        (k, v)
        for k, v in request.headers.items()
        if k.lower() not in _DROP_REQUEST_HEADERS and k.lower() != config.agent_header
    ]
    # Force identity encoding upstream. Stripping the client's Accept-Encoding is
    # not enough: httpx adds its own `Accept-Encoding: gzip, deflate` default, so
    # the upstream would compress, and we forward raw body bytes verbatim without
    # re-compressing. Setting identity explicitly overrides that default — keeping
    # passthrough byte-faithful AND the body readable for usage metering.
    fwd_headers.append(("accept-encoding", "identity"))

    raw_path = request.url.path
    if request.url.query:
        raw_path = f"{raw_path}?{request.url.query}"

    upstream_req = client.build_request(
        request.method, raw_path, headers=fwd_headers, content=body
    )
    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        # A genuine upstream failure (not a metering failure) — surface as 502.
        log.warning("upstream request failed: %s", exc)
        return Response(b"tokenwarden: upstream request failed", status_code=502)

    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _DROP_RESPONSE_HEADERS
    }
    request_id = upstream_resp.headers.get("request-id")
    meter = _Meter(upstream_resp.headers.get("content-type", ""))

    async def stream():
        try:
            async for chunk in upstream_resp.aiter_raw():
                meter.feed(chunk)
                yield chunk
        finally:
            try:
                await upstream_resp.aclose()
            finally:
                await _meter_and_alert(storage, config, alerts, agent_id, request_id, meter)

    return StreamingResponse(
        stream(), status_code=upstream_resp.status_code, headers=resp_headers
    )


# Anthropic uses POST/GET; the rest are accepted so the proxy is transparent.
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def create_app(
    config: Config,
    storage: Storage,
    upstream_client: httpx.AsyncClient | None = None,
    alert_manager: AlertManager | None = None,
) -> Starlette:
    client = upstream_client or httpx.AsyncClient(
        base_url=config.upstream_url, timeout=httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS)
    )
    alerts = alert_manager or AlertManager(config, storage, build_notifier(config))
    @asynccontextmanager
    async def lifespan(_app: Starlette):
        # Close the upstream client when the server stops.
        try:
            yield
        finally:
            await client.aclose()

    app = Starlette(
        routes=[Route("/{path:path}", _proxy, methods=_METHODS)], lifespan=lifespan
    )
    app.state.config = config
    app.state.storage = storage
    app.state.upstream = client
    app.state.alerts = alerts
    return app
