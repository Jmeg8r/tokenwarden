import logging

from tokenwarden.config import DEFAULT_PRICES
from tokenwarden.models import Usage
from tokenwarden.pricing import cost_usd


def test_cost_input_and_output():
    usage = Usage(model="claude-opus-4-8", input_tokens=1_000_000, output_tokens=1_000_000)
    # Opus 4.8: $5 input + $25 output per MTok
    assert cost_usd(usage, DEFAULT_PRICES) == 30.0


def test_cost_includes_cache():
    usage = Usage(
        model="claude-sonnet-4-6",
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )
    # Sonnet 4.6: cache_read 0.3 + cache_write 3.75 per MTok
    assert round(cost_usd(usage, DEFAULT_PRICES), 6) == round(0.3 + 3.75, 6)


def test_unknown_model_is_zero_and_warns(caplog):
    usage = Usage(model="claude-imaginary-9", input_tokens=1_000_000)
    with caplog.at_level(logging.WARNING):
        assert cost_usd(usage, DEFAULT_PRICES) == 0.0
    assert any("no price" in r.message for r in caplog.records)


def test_no_model_is_zero():
    assert cost_usd(Usage(input_tokens=1_000_000), DEFAULT_PRICES) == 0.0
