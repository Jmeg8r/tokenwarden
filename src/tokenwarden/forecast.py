"""Forward-looking spend forecasting — projected-overrun warnings and
runaway-agent anomaly detection.

This module is deliberately isolated from the serving path: `gateway.py` never
imports it, and the heavy `timesfm`/`torch` backend is imported lazily *inside*
`TimesFMForecaster.__init__` so merely importing this module (or running the
gateway) never pulls torch into memory. Forecasting is an offline reader of the
same SQLite spend log.

Two backends implement the `Forecaster` protocol:
  - `NaiveForecaster` — stdlib-only seasonal-naive baseline (always available,
    CI-testable, and the graceful fallback when the timesfm extra is absent).
  - `TimesFMForecaster` — zero-shot TimesFM 2.5 foundation model (optional extra).

The series they consume is a regular hourly spend series (see
`Storage.hourly_spend`); the daily seasonal period is therefore 24.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from statistics import NormalDist
from typing import Protocol

from tokenwarden.config import Config
from tokenwarden.models import Alert

log = logging.getLogger("tokenwarden.forecast")

SEASON = 24  # hourly buckets → a 24-step daily cycle


@dataclass(slots=True)
class Forecast:
    """A horizon-step forecast with a lower/upper quantile band per step.

    `low_confidence` is set when there isn't enough history for a real fit — the
    numbers are still returned, but callers should surface the caveat (the SPEC's
    "be honest about what it can and cannot see").
    """

    point: list[float]
    lower: list[float]
    upper: list[float]
    low_confidence: bool = False

    def sums(self) -> tuple[float, float, float]:
        """Total point/lower/upper over the whole horizon."""
        return sum(self.point), sum(self.lower), sum(self.upper)


@dataclass(slots=True)
class Projection:
    """End-of-period projection: observed-so-far plus the forecast remainder."""

    point: float
    lower: float
    upper: float


class Forecaster(Protocol):
    def forecast(self, history: list[float], horizon: int) -> Forecast: ...


def _z_for_quantile(quantile: float) -> float:
    """One-sided normal z for the band edge (e.g. 0.9 → ~1.2816)."""
    return NormalDist().inv_cdf(quantile)


class NaiveForecaster:
    """Seasonal-naive baseline: each future hour is the mean of the same
    phase-of-cycle across every prior cycle, with a symmetric band from the
    empirical residual spread. No dependencies beyond the stdlib.
    """

    def __init__(self, quantile: float = 0.9, min_history_hours: int = 48) -> None:
        self._z = _z_for_quantile(quantile)
        self._min_history_hours = min_history_hours

    def _seasonal_point(self, history: list[float], n: int, k: int) -> float:
        """Predict step k (0-indexed into the horizon) as the mean of the same
        phase in earlier cycles. Phase is aligned by absolute index, so no
        knowledge of wall-clock hour is needed."""
        idx = n + k
        samples = [
            history[idx - SEASON * c]
            for c in range(1, idx // SEASON + 1)
            if 0 <= idx - SEASON * c < n
        ]
        return statistics.fmean(samples) if samples else statistics.fmean(history)

    def _residual_sigma(self, history: list[float], n: int) -> float:
        resids: list[float] = []
        for i in range(SEASON, n):
            samples = [
                history[i - SEASON * c] for c in range(1, i // SEASON + 1) if 0 <= i - SEASON * c < i
            ]
            if samples:
                resids.append(history[i] - statistics.fmean(samples))
        return statistics.pstdev(resids) if len(resids) > 1 else 0.0

    def forecast(self, history: list[float], horizon: int) -> Forecast:
        n = len(history)
        low_conf = n < max(2 * SEASON, self._min_history_hours)
        if horizon <= 0:
            return Forecast([], [], [], low_confidence=low_conf)
        if n == 0:
            return Forecast([0.0] * horizon, [0.0] * horizon, [0.0] * horizon, low_confidence=True)
        if n < 2 * SEASON:
            # Too little to fit a daily cycle — flat mean with the sample spread.
            base = statistics.fmean(history)
            point = [base] * horizon
            sigma = statistics.pstdev(history) if n > 1 else 0.0
        else:
            point = [self._seasonal_point(history, n, k) for k in range(horizon)]
            sigma = self._residual_sigma(history, n)
        lower = [max(0.0, p - self._z * sigma) for p in point]
        upper = [p + self._z * sigma for p in point]
        return Forecast(point, lower, upper, low_confidence=low_conf)


class TimesFMForecaster:
    """Zero-shot TimesFM 2.5 forecaster. Imports torch/timesfm lazily so this
    class only costs anything when actually constructed. Requires the optional
    `forecast` extra (`pip install tokenwarden[forecast]`).
    """

    def __init__(
        self,
        quantile: float = 0.9,
        checkpoint: str = "google/timesfm-2.5-200m-pytorch",
        min_history_hours: int = 48,
        max_context: int = 1024,
        max_horizon: int = 256,
    ) -> None:
        import numpy as np  # noqa: F401  (heavy — kept out of module import)
        import timesfm

        self._np = np
        self._quantile = quantile
        self._min_history_hours = min_history_hours
        model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(checkpoint)
        model.compile(
            timesfm.ForecastConfig(
                max_context=max_context,
                max_horizon=max_horizon,
                use_continuous_quantile_head=True,
            )
        )
        self._model = model

    def forecast(self, history: list[float], horizon: int) -> Forecast:
        low_conf = len(history) < self._min_history_hours
        if horizon <= 0 or not history:
            zeros = [0.0] * max(horizon, 0)
            return Forecast(list(zeros), list(zeros), list(zeros), low_confidence=True)
        np = self._np
        arr = np.asarray(history, dtype=float)
        point_forecast, quantile_forecast = self._model.forecast(horizon=horizon, inputs=[arr])
        point = [float(v) for v in np.asarray(point_forecast)[0][:horizon]]
        # quantile_forecast[0] is (horizon, n) with column 0 the mean and the
        # remaining columns ascending quantiles (TimesFM 2.5: mean, then 10th..90th).
        q = np.asarray(quantile_forecast)[0]
        cols = q.shape[-1] - 1  # drop the leading mean column
        lo_col = 1 + _band_col(1.0 - self._quantile, cols)
        hi_col = 1 + _band_col(self._quantile, cols)
        lower = [max(0.0, float(q[h][lo_col])) for h in range(horizon)]
        upper = [float(q[h][hi_col]) for h in range(horizon)]
        return Forecast(point, lower, upper, low_confidence=low_conf)


def _band_col(quantile: float, cols: int) -> int:
    """Nearest quantile column for a decile grid spanning 0.1..(cols/10)."""
    idx = round(quantile * 10) - 1  # 0.1→col0, 0.9→col8
    return max(0, min(cols - 1, idx))


def build_forecaster(cfg: Config) -> Forecaster:
    """Select a forecaster from config. A requested `timesfm` backend that can't
    be constructed (extra not installed, checkpoint download fails) degrades to
    the naive baseline with a warning rather than crashing — mirroring
    `build_notifier`'s missing-secret behavior."""
    fc = cfg.forecasting
    if fc.backend == "timesfm":
        try:
            return TimesFMForecaster(
                quantile=fc.quantile,
                checkpoint=fc.checkpoint,
                min_history_hours=fc.min_history_hours,
            )
        except Exception:  # noqa: BLE001 — any load failure → degrade, don't crash
            log.warning(
                "timesfm backend unavailable (install `tokenwarden[forecast]`); "
                "falling back to the naive forecaster"
            )
    return NaiveForecaster(quantile=fc.quantile, min_history_hours=fc.min_history_hours)


def project_end_of_day(observed_today: float, remaining: Forecast) -> Projection:
    """Observed spend so far today plus the forecast of the remaining hours."""
    point, lower, upper = remaining.sums()
    return Projection(
        point=observed_today + point,
        lower=observed_today + lower,
        upper=observed_today + upper,
    )


def evaluate_overrun(
    scope: str,
    day: str,
    projection: Projection,
    budget: float | None,
    warn_pct: float,
    critical_pct: float,
) -> Alert | None:
    """Warn when the *projected* end-of-day spend (upper band) would breach the
    budget — the whole point of forecasting is to fire before it lands. Returns
    a `kind="projected"` Alert, or None when nothing is budgeted / below warn."""
    if not budget or budget <= 0:
        return None
    projected = projection.upper  # upper band → catch likely overruns early
    pct = projected / budget * 100.0
    if pct >= critical_pct:
        level = "critical"
    elif pct >= warn_pct:
        level = "warn"
    else:
        return None
    return Alert(
        scope=scope, level=level, spent=projected, budget=budget, pct=pct, day=day, kind="projected"
    )


def evaluate_anomaly(
    scope: str,
    day: str,
    history: list[float],
    actual: float,
    forecaster: Forecaster,
    factor: float,
) -> Alert | None:
    """Flag a completed hour whose actual spend punches above `factor` times the
    one-step-ahead forecast band — a stuck loop / runaway agent. `history` must
    exclude the bucket under test. Returns a `kind="anomaly"` Alert or None."""
    if len(history) < 2 or actual <= 0:
        return None
    fc = forecaster.forecast(history, 1)
    band = fc.upper[0] if fc.upper else 0.0
    if band <= 0 and max(history, default=0.0) <= 0:
        # No spend signal at all in the baseline (idle / newly-onboarded agent) —
        # nothing to compare against, so a first charge is not an "anomaly".
        return None
    threshold = band * factor
    if actual <= threshold:
        return None
    pct = (actual / band * 100.0) if band > 0 else float("inf")
    return Alert(
        scope=scope, level="critical", spent=actual, budget=band, pct=pct, day=day, kind="anomaly"
    )
