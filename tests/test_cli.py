from tokenwarden.cli import main


def test_status_accepts_config_after_subcommand(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[gateway]\ndb_path = "{tmp_path / "x.db"}"\n')
    # Regression: `--config` must be accepted *after* the subcommand
    # (`tokenwarden status --config foo.toml`), not only before it.
    assert main(["status", "--config", str(cfg)]) == 0
