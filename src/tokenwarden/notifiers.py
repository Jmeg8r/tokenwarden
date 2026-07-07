"""Alert delivery channels. Secrets come from environment variables, not config."""

from __future__ import annotations

import logging
import os
from typing import Protocol

import httpx

from tokenwarden.config import Config
from tokenwarden.models import Alert

log = logging.getLogger("tokenwarden.notifiers")

NOTIFY_TIMEOUT_SECONDS = 10.0

# Env var names for channel secrets (deliberately kept out of the config file).
ENV_DISCORD_WEBHOOK = "TOKENWARDEN_DISCORD_WEBHOOK_URL"
ENV_TELEGRAM_TOKEN = "TOKENWARDEN_TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT = "TOKENWARDEN_TELEGRAM_CHAT_ID"
# Optional: send to a specific forum topic (thread) within the chat. When set, the
# value is passed as `message_thread_id` so alerts land in that topic, not General.
ENV_TELEGRAM_THREAD = "TOKENWARDEN_TELEGRAM_THREAD_ID"


def format_alert(alert: Alert) -> str:
    marker = "[CRITICAL]" if alert.level == "critical" else "[WARN]"
    who = "Global" if alert.scope == "global" else f"Agent '{alert.scope.split(':', 1)[1]}'"
    if alert.kind == "projected":
        return (
            f"{marker} tokenwarden: {who} {alert.period} spend projected to reach "
            f"${alert.spent:,.2f} of ${alert.budget:,.2f} budget "
            f"({alert.pct:.0f}%) by end of {alert.day}"
        )
    if alert.kind == "anomaly":
        return (
            f"{marker} tokenwarden: {who} anomalous spend ${alert.spent:,.2f} "
            f"exceeds the forecast band of ${alert.budget:,.2f} on {alert.day}"
        )
    return (
        f"{marker} tokenwarden: {who} {alert.period} spend "
        f"${alert.spent:,.2f} of ${alert.budget:,.2f} budget "
        f"({alert.pct:.0f}%) on {alert.day}"
    )


class Notifier(Protocol):
    async def notify(self, alert: Alert) -> None: ...


class NullNotifier:
    """Used when no channel is configured (or a secret is missing)."""

    async def notify(self, alert: Alert) -> None:
        return None


async def _post(client: httpx.AsyncClient | None, url: str, payload: dict, channel: str) -> None:
    """POST a notification, swallowing all errors — a down webhook must never
    break the request or the metering path."""
    try:
        if client is not None:
            resp = await client.post(url, json=payload)
        else:
            async with httpx.AsyncClient(timeout=NOTIFY_TIMEOUT_SECONDS) as owned:
                resp = await owned.post(url, json=payload)
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        log.exception("%s notification failed", channel)


class DiscordNotifier:
    def __init__(self, webhook_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = webhook_url
        self._client = client

    async def notify(self, alert: Alert) -> None:
        await _post(self._client, self._url, {"content": format_alert(alert)}, "Discord")


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        thread_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._client = client

    async def notify(self, alert: Alert) -> None:
        payload = {"chat_id": self._chat_id, "text": format_alert(alert)}
        if self._thread_id:
            payload["message_thread_id"] = self._thread_id
        await _post(self._client, self._url, payload, "Telegram")


class MultiNotifier:
    def __init__(self, notifiers: list) -> None:
        self._notifiers = notifiers

    async def notify(self, alert: Alert) -> None:
        for notifier in self._notifiers:
            await notifier.notify(alert)  # each notifier guards its own errors


def build_notifier(config: Config) -> Notifier:
    """Build a notifier from the configured channels, reading secrets from env.
    A channel whose secret is absent is skipped with a warning (not fatal)."""
    notifiers: list = []
    for channel in config.notifier_channels:
        if channel == "discord":
            url = os.environ.get(ENV_DISCORD_WEBHOOK)
            if url:
                notifiers.append(DiscordNotifier(url))
            else:
                log.warning("notifier 'discord' enabled but %s is not set — skipping", ENV_DISCORD_WEBHOOK)
        elif channel == "telegram":
            token = os.environ.get(ENV_TELEGRAM_TOKEN)
            chat = os.environ.get(ENV_TELEGRAM_CHAT)
            thread = os.environ.get(ENV_TELEGRAM_THREAD)  # optional forum-topic id
            if token and chat:
                notifiers.append(TelegramNotifier(token, chat, thread_id=thread))
            else:
                log.warning(
                    "notifier 'telegram' enabled but %s/%s not set — skipping",
                    ENV_TELEGRAM_TOKEN,
                    ENV_TELEGRAM_CHAT,
                )
    if not notifiers:
        return NullNotifier()
    return notifiers[0] if len(notifiers) == 1 else MultiNotifier(notifiers)
