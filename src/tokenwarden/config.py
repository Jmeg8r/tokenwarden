"""Configuration schema, defaults, and TOML loading.

WHY a config object instead of constants: Anthropic's billing is in flux, so the
price table in particular must be user-overridable at runtime — a hardcoded table
would silently produce wrong numbers the next time rates change.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --- defaults (no magic numbers scattered through the code) ---
DEFAULT_UPSTREAM = "https://api.anthropic.com"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788  # 8787 is commonly taken by Headroom's proxy; avoid the clash
DEFAULT_TZ = "America/New_York"
DEFAULT_DB = "tokenwarden.db"
DEFAULT_AGENT_HEADER = "x-watchdog-agent"
DEFAULT_WARN_PCT = 80.0
DEFAULT_CRITICAL_PCT = 100.0

# Cache pricing is derived from input price by these multipliers when not given
# explicitly: reads are ~0.1x input, 5-minute writes ~1.25x input.
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25


@dataclass(slots=True, frozen=True)
class Price:
    """USD per 1,000,000 tokens for one model."""

    input: float
    output: float
    cache_read: float
    cache_write: float


def _price(inp: float, out: float) -> Price:
    return Price(
        input=inp,
        output=out,
        cache_read=round(inp * _CACHE_READ_MULT, 6),
        cache_write=round(inp * _CACHE_WRITE_MULT, 6),
    )


# Current-model defaults (USD/MTok). Override or extend in config.toml.
DEFAULT_PRICES: dict[str, Price] = {
    "claude-fable-5": _price(10.0, 50.0),
    "claude-opus-4-8": _price(5.0, 25.0),
    "claude-opus-4-7": _price(5.0, 25.0),
    "claude-opus-4-6": _price(5.0, 25.0),
    "claude-sonnet-4-6": _price(3.0, 15.0),
    "claude-haiku-4-5": _price(1.0, 5.0),
}


@dataclass(slots=True)
class Budgets:
    global_daily: float | None = None
    global_monthly: float | None = None
    default_agent_daily: float | None = None
    per_agent_daily: dict[str, float] = field(default_factory=dict)

    def daily_for(self, agent_id: str) -> float | None:
        """Resolve an agent's daily budget: explicit override, else the default."""
        return self.per_agent_daily.get(agent_id, self.default_agent_daily)


@dataclass(slots=True)
class Config:
    upstream_url: str = DEFAULT_UPSTREAM
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    timezone: str = DEFAULT_TZ
    db_path: str = DEFAULT_DB
    agent_header: str = DEFAULT_AGENT_HEADER
    prices: dict[str, Price] = field(default_factory=lambda: dict(DEFAULT_PRICES))
    budgets: Budgets = field(default_factory=Budgets)
    warn_pct: float = DEFAULT_WARN_PCT
    critical_pct: float = DEFAULT_CRITICAL_PCT
    enforce: bool = False
    debug_log_bodies: bool = False
    notifier_channels: list[str] = field(default_factory=list)

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def validate(self) -> None:
        if not self.upstream_url.startswith(("http://", "https://")):
            raise ValueError(f"upstream_url must be http(s): {self.upstream_url!r}")
        if not 0 < self.port < 65536:
            raise ValueError(f"port out of range: {self.port}")
        if not 0 < self.warn_pct <= self.critical_pct:
            raise ValueError("require 0 < warn_pct <= critical_pct")
        bad_channels = set(self.notifier_channels) - {"discord", "telegram"}
        if bad_channels:
            raise ValueError(f"unknown notifier channels: {sorted(bad_channels)}")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone!r}") from exc

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        """Load config from a TOML file, layered over defaults."""
        data = tomllib.loads(Path(path).read_text("utf-8"))
        cfg = cls()

        gw = data.get("gateway", {})
        cfg.upstream_url = gw.get("upstream_url", cfg.upstream_url)
        cfg.host = gw.get("host", cfg.host)
        cfg.port = int(gw.get("port", cfg.port))
        cfg.timezone = gw.get("timezone", cfg.timezone)
        cfg.db_path = gw.get("db_path", cfg.db_path)
        # HTTP header names are case-insensitive; normalize so lookups match.
        cfg.agent_header = gw.get("agent_header", cfg.agent_header).lower()
        cfg.enforce = bool(gw.get("enforce", cfg.enforce))
        cfg.debug_log_bodies = bool(gw.get("debug_log_bodies", cfg.debug_log_bodies))

        th = data.get("thresholds", {})
        cfg.warn_pct = float(th.get("warn_pct", cfg.warn_pct))
        cfg.critical_pct = float(th.get("critical_pct", cfg.critical_pct))

        cfg.notifier_channels = list(data.get("notifier", {}).get("channels", []))

        for model, row in data.get("prices", {}).items():
            inp = float(row["input"])
            cfg.prices[model] = Price(
                input=inp,
                output=float(row["output"]),
                cache_read=float(row.get("cache_read", round(inp * _CACHE_READ_MULT, 6))),
                cache_write=float(row.get("cache_write", round(inp * _CACHE_WRITE_MULT, 6))),
            )

        b = data.get("budgets", {})
        cfg.budgets = Budgets(
            global_daily=b.get("global_daily"),
            global_monthly=b.get("global_monthly"),
            default_agent_daily=b.get("default_agent_daily"),
            per_agent_daily={k: float(v) for k, v in b.get("per_agent_daily", {}).items()},
        )

        cfg.validate()
        return cfg
