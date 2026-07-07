"""Shared data structures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class Usage:
    """Token usage extracted from one Anthropic Messages response.

    Token counts mirror the API's `usage` object; `cache_creation_tokens` and
    `cache_read_tokens` map to `cache_creation_input_tokens` /
    `cache_read_input_tokens`.
    """

    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    service_tier: str | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.input_tokens == 0
            and self.output_tokens == 0
            and self.cache_creation_tokens == 0
            and self.cache_read_tokens == 0
        )


@dataclass(slots=True)
class Alert:
    """A budget signal, ready to be rendered into a notification.

    `kind` distinguishes the three signal types that share this shape:
      - "budget"    — today's *actual* spend crossed a threshold (backward-looking).
      - "projected" — today's spend is *forecast* to breach the budget (`spent` is
                      the projected end-of-day total).
      - "anomaly"   — actual spend punched above the forecast's upper band (`budget`
                      carries the band the value exceeded).
    """

    scope: str  # "global" or "agent:<id>"
    level: str  # "warn" | "critical"
    spent: float
    budget: float
    pct: float
    day: str  # local YYYY-MM-DD
    period: str = "daily"
    kind: Literal["budget", "projected", "anomaly"] = "budget"
