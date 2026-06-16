"""Shared data structures."""

from __future__ import annotations

from dataclasses import dataclass


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
