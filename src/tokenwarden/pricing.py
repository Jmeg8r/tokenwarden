"""Estimate USD cost for a response from token usage and the price table."""

from __future__ import annotations

import logging

from tokenwarden.config import Price
from tokenwarden.models import Usage

log = logging.getLogger("tokenwarden.pricing")

_PER_MILLION = 1_000_000


def cost_usd(usage: Usage, prices: dict[str, Price]) -> float:
    """Estimated USD cost for one response.

    An unknown model returns 0.0 *and logs a warning* — we never silently drop
    spend. A missing price means the table is stale, which is exactly the kind of
    billing drift this tool exists to surface.
    """
    if usage.model is None:
        return 0.0
    price = prices.get(usage.model)
    if price is None:
        log.warning(
            "no price for model %r — counting $0; update the price table", usage.model
        )
        return 0.0
    return (
        usage.input_tokens / _PER_MILLION * price.input
        + usage.output_tokens / _PER_MILLION * price.output
        + usage.cache_read_tokens / _PER_MILLION * price.cache_read
        + usage.cache_creation_tokens / _PER_MILLION * price.cache_write
    )
