import json

from tokenwarden.usage import SSEUsageAccumulator, parse_json_usage


def test_parse_json_usage():
    body = json.dumps(
        {
            "type": "message",
            "model": "claude-opus-4-8",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
            },
        }
    ).encode()
    u = parse_json_usage(body)
    assert u is not None
    assert u.model == "claude-opus-4-8"
    assert u.input_tokens == 100 and u.output_tokens == 50
    assert u.cache_read_tokens == 10 and u.cache_creation_tokens == 5


def test_parse_json_usage_invalid():
    assert parse_json_usage(b"not json") is None
    assert parse_json_usage(b'{"no":"usage here"}') is None


def _sse(events: list[dict]) -> bytes:
    out = b""
    for e in events:
        out += b"event: " + e["type"].encode() + b"\n"
        out += b"data: " + json.dumps(e).encode() + b"\n\n"
    return out


def test_sse_accumulator_full_stream():
    stream = _sse(
        [
            {
                "type": "message_start",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 80}},
        ]
    )
    acc = SSEUsageAccumulator()
    acc.feed(stream)
    u = acc.result()
    assert u is not None
    assert u.model == "claude-sonnet-4-6"
    assert u.input_tokens == 200 and u.output_tokens == 80 and u.cache_read_tokens == 20


def test_sse_accumulator_handles_split_chunks():
    stream = _sse(
        [
            {
                "type": "message_start",
                "message": {"model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": 1}},
            },
            {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 7}},
        ]
    )
    acc = SSEUsageAccumulator()
    # Feed one byte at a time to prove the buffer handles arbitrary boundaries.
    for i in range(len(stream)):
        acc.feed(stream[i : i + 1])
    u = acc.result()
    assert u is not None
    assert u.input_tokens == 10 and u.output_tokens == 7


def test_sse_no_usage_returns_none():
    acc = SSEUsageAccumulator()
    acc.feed(b'event: ping\ndata: {"type":"ping"}\n\n')
    assert acc.result() is None
