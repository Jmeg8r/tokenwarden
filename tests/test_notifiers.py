import asyncio
import json
import logging

import httpx

from tokenwarden.config import Config
from tokenwarden.models import Alert
from tokenwarden.notifiers import (
    DiscordNotifier,
    NullNotifier,
    TelegramNotifier,
    build_notifier,
    format_alert,
)


def _alert():
    return Alert(scope="agent:forge", level="critical", spent=12.0, budget=10.0, pct=120.0, day="2026-06-16")


def test_format_alert_mentions_key_facts():
    msg = format_alert(_alert())
    assert "forge" in msg and "120%" in msg and "tokenwarden" in msg.lower()


def test_discord_posts_content():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = DiscordNotifier("https://discord.test/webhook", client=client)
    asyncio.run(notifier.notify(_alert()))
    asyncio.run(client.aclose())

    assert captured["url"] == "https://discord.test/webhook"
    assert "tokenwarden" in captured["body"]["content"].lower()


def test_telegram_posts_sendmessage():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier("TOKEN", "CHAT", client=client)
    asyncio.run(notifier.notify(_alert()))
    asyncio.run(client.aclose())

    assert "/botTOKEN/sendMessage" in captured["url"]
    assert captured["body"]["chat_id"] == "CHAT"
    assert "message_thread_id" not in captured["body"]  # omitted when no thread set


def test_telegram_includes_thread_id_when_set():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier("TOKEN", "CHAT", thread_id="25", client=client)
    asyncio.run(notifier.notify(_alert()))
    asyncio.run(client.aclose())

    assert captured["body"]["chat_id"] == "CHAT"
    assert captured["body"]["message_thread_id"] == "25"  # lands in the forum topic


def test_notifier_swallows_http_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = DiscordNotifier("https://discord.test/webhook", client=client)
    # Must not raise even though the webhook returns 500 (alerting is best-effort).
    asyncio.run(notifier.notify(_alert()))
    asyncio.run(client.aclose())


def test_build_notifier_from_env(monkeypatch):
    monkeypatch.setenv("TOKENWARDEN_DISCORD_WEBHOOK_URL", "https://discord.test/wh")
    notifier = build_notifier(Config(notifier_channels=["discord"]))
    assert isinstance(notifier, DiscordNotifier)


def test_build_notifier_missing_secret_is_null(monkeypatch, caplog):
    monkeypatch.delenv("TOKENWARDEN_DISCORD_WEBHOOK_URL", raising=False)
    with caplog.at_level(logging.WARNING):
        notifier = build_notifier(Config(notifier_channels=["discord"]))
    assert isinstance(notifier, NullNotifier)
    assert any("discord" in r.message.lower() for r in caplog.records)
