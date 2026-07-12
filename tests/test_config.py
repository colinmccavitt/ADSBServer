"""Tests for app.config — plain config load/save plus the gitignored
secrets-file split (config.secrets.json) that keeps API keys out of the
tracked config.json."""

import json

from app import config as cfg


def test_load_returns_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    result = cfg.load()
    assert result["latitude"] == cfg.DEFAULTS["latitude"]
    assert result["http_port"] == 8080
    assert "api_keys" not in result


def test_load_merges_saved_values(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"latitude": 1.23, "watchlist": ["N1"]}))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(config_path))

    result = cfg.load()
    assert result["latitude"] == 1.23
    assert result["watchlist"] == ["N1"]
    assert result["longitude"] == cfg.DEFAULTS["longitude"]  # untouched default


def test_load_strips_legacy_api_keys_block(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "latitude": 1.0,
        "api_keys": {"collector_keys": ["secret-1"], "client_keys": ["secret-2"]},
    }))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(config_path))

    result = cfg.load()
    assert "api_keys" not in result


def test_save_never_persists_secret_keys(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(config_path))

    cfg.save({
        "latitude": 5.0,
        "longitude": 6.0,
        "api_keys": {"collector_keys": ["should-not-be-written"]},
    })

    on_disk = json.loads(config_path.read_text())
    assert "api_keys" not in on_disk
    assert on_disk["latitude"] == 5.0


def test_load_secrets_reads_dedicated_secrets_file(tmp_path, monkeypatch):
    secrets_path = tmp_path / "config.secrets.json"
    secrets_path.write_text(json.dumps({
        "collector_keys": ["c1"],
        "client_keys": ["k1", "k2"],
    }))
    monkeypatch.setattr(cfg, "SECRETS_PATH", str(secrets_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))

    secrets = cfg.load_secrets()
    assert secrets["collector_keys"] == ["c1"]
    assert secrets["client_keys"] == ["k1", "k2"]


def test_load_secrets_falls_back_to_legacy_config_json(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "api_keys": {"collector_keys": ["legacy-key"], "client_keys": []},
    }))
    monkeypatch.setattr(cfg, "SECRETS_PATH", str(tmp_path / "config.secrets.json"))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(config_path))

    secrets = cfg.load_secrets()
    assert secrets["collector_keys"] == ["legacy-key"]


def test_load_secrets_returns_empty_when_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "SECRETS_PATH", str(tmp_path / "config.secrets.json"))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))

    assert cfg.load_secrets() == {}
