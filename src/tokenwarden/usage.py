"""Extract token usage from Anthropic Messages responses.

Two shapes:
- non-streaming JSON: usage is in the response body's `usage` object.
- streaming SSE: input/cache tokens arrive in `message_start`, and the final
  `output_tokens` arrives in `message_delta`.
"""

from __future__ import annotations

import json
import logging

from tokenwarden.models import Usage

log = logging.getLogger("tokenwarden.usage")


def _usage_from_message(message: dict) -> Usage:
    u = message.get("usage") or {}
    return Usage(
        model=message.get("model"),
        input_tokens=int(u.get("input_tokens", 0) or 0),
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_creation_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
    )


def parse_json_usage(body: bytes) -> Usage | None:
    """Extract usage from a non-streaming Messages response body."""
    try:
        message = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(message, dict) or "usage" not in message:
        return None
    return _usage_from_message(message)


class SSEUsageAccumulator:
    """Incrementally parse an Anthropic SSE stream to recover final usage.

    Chunk boundaries fall anywhere, so we buffer bytes and only act on complete
    lines — never assume one network chunk equals one SSE event.
    """

    def __init__(self) -> None:
        self._buf = b""
        self._usage = Usage()
        self._seen = False

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[len(b"data:") :].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            self._consume(event)

    def _consume(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "message_start":
            base = _usage_from_message(event.get("message") or {})
            # message_start carries input + cache + model; output is ~1 here and
            # is superseded by the final message_delta.
            self._usage.model = base.model
            self._usage.input_tokens = base.input_tokens
            self._usage.cache_creation_tokens = base.cache_creation_tokens
            self._usage.cache_read_tokens = base.cache_read_tokens
            self._seen = True
        elif etype == "message_delta":
            u = event.get("usage") or {}
            if "output_tokens" in u:
                self._usage.output_tokens = int(u["output_tokens"] or 0)
                self._seen = True

    def result(self) -> Usage | None:
        return self._usage if self._seen else None
