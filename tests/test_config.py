import pytest

from tokenwarden.config import Config


def _write(tmp_path, body: str):
    p = tmp_path / "c.toml"
    p.write_text(body)
    return str(p)


def test_forecasting_defaults_when_section_absent(tmp_path):
    cfg = Config.load(_write(tmp_path, '[gateway]\nport = 8788\n'))
    fc = cfg.forecasting
    assert fc.enabled is False
    assert fc.backend == "naive"
    assert fc.lookback_days == 14
    assert fc.quantile == 0.9


def test_forecasting_section_parsed(tmp_path):
    cfg = Config.load(
        _write(
            tmp_path,
            "[forecasting]\n"
            "enabled = true\n"
            'backend = "timesfm"\n'
            "lookback_days = 30\n"
            "quantile = 0.95\n"
            "anomaly_factor = 2.0\n",
        )
    )
    fc = cfg.forecasting
    assert fc.enabled is True
    assert fc.backend == "timesfm"
    assert fc.lookback_days == 30
    assert fc.quantile == 0.95
    assert fc.anomaly_factor == 2.0


@pytest.mark.parametrize(
    "body",
    [
        '[forecasting]\nbackend = "prophet"\n',   # unknown backend
        "[forecasting]\nquantile = 1.5\n",        # out of (0,1)
        "[forecasting]\nlookback_days = 0\n",     # must be > 0
    ],
)
def test_forecasting_invalid_config_rejected(tmp_path, body):
    with pytest.raises(ValueError):
        Config.load(_write(tmp_path, body))
