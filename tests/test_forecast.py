import pytest

from tokenwarden.config import Config, Forecasting
from tokenwarden.forecast import (
    NaiveForecaster,
    Projection,
    build_forecaster,
    evaluate_anomaly,
    evaluate_overrun,
    project_end_of_day,
)


def _diurnal(cycles: int, spike_hour: int = 12, base: float = 0.1, spike: float = 5.0):
    """`cycles` identical 24h days with a single spike hour — a clean, noise-free
    seasonal signal the seasonal-naive forecaster should reproduce exactly."""
    return [
        base + (spike if h == spike_hour else 0.0)
        for _ in range(cycles)
        for h in range(24)
    ]


def test_naive_reproduces_clean_daily_cycle():
    history = _diurnal(cycles=3)  # 72 points, phase-aligned to hour 0
    fc = NaiveForecaster(quantile=0.9, min_history_hours=48).forecast(history, horizon=24)
    assert len(fc.point) == 24
    assert fc.point[0] == pytest.approx(0.1)   # quiet hour
    assert fc.point[12] == pytest.approx(5.1)  # spike hour
    # Identical cycles → zero residual → collapsed band.
    assert fc.lower[12] == pytest.approx(5.1)
    assert fc.upper[12] == pytest.approx(5.1)
    assert fc.low_confidence is False


def test_naive_flat_series_is_low_confidence_with_flat_mean():
    fc = NaiveForecaster(min_history_hours=48).forecast([2.0] * 10, horizon=6)
    assert fc.point == [2.0] * 6
    assert fc.low_confidence is True  # < 48h of history


def test_naive_zero_horizon_returns_empty():
    fc = NaiveForecaster().forecast([1.0, 2.0, 3.0], horizon=0)
    assert fc.point == [] and fc.lower == [] and fc.upper == []


def test_project_end_of_day_adds_observed():
    from tokenwarden.forecast import Forecast

    remaining = Forecast(point=[1.0, 1.0, 1.0], lower=[0.5, 0.5, 0.5], upper=[2.0, 2.0, 2.0])
    proj = project_end_of_day(observed_today=5.0, remaining=remaining)
    assert proj == Projection(point=8.0, lower=6.5, upper=11.0)


@pytest.mark.parametrize(
    "budget,expect_level",
    [(10.0, "critical"), (13.0, "warn"), (20.0, None), (None, None)],
)
def test_evaluate_overrun_triggers_on_upper_band(budget, expect_level):
    proj = Projection(point=8.0, lower=6.5, upper=11.0)  # upper 11 is the trigger
    alert = evaluate_overrun("global", "2026-07-06", proj, budget, warn_pct=80, critical_pct=100)
    if expect_level is None:
        assert alert is None
    else:
        assert alert is not None
        assert alert.level == expect_level
        assert alert.kind == "projected"
        assert alert.spent == pytest.approx(11.0)


def test_evaluate_anomaly_flags_spike_above_band():
    forecaster = NaiveForecaster(quantile=0.9, min_history_hours=48)
    history = [1.0] * 48  # steady baseline → tight band at ~1.0
    spike = evaluate_anomaly("agent:forge", "2026-07-06", history, actual=5.0,
                             forecaster=forecaster, factor=1.5)
    assert spike is not None and spike.kind == "anomaly" and spike.level == "critical"
    # A value within factor× the band is not anomalous.
    normal = evaluate_anomaly("agent:forge", "2026-07-06", history, actual=1.2,
                              forecaster=forecaster, factor=1.5)
    assert normal is None


def test_build_forecaster_defaults_to_naive():
    cfg = Config(forecasting=Forecasting(backend="naive"))
    assert isinstance(build_forecaster(cfg), NaiveForecaster)
