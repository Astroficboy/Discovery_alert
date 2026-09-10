"""Configuration loading and validation."""

from __future__ import annotations

import pytest
import yaml

from src.config import load_config


def test_default_config_loads():
    config = load_config(load_dotenv_file=False)
    assert config.newsletter.frequency_days == 2
    assert "music" in config.content.categories
    assert config.pipeline.prefilter_keep <= config.pipeline.discovery_target


def test_scoring_weights_are_normalised():
    config = load_config(load_dotenv_file=False)
    assert sum(config.scoring.weights.values()) == pytest.approx(1.0)
    assert sum(config.scoring.music_weights.values()) == pytest.approx(1.0)


def test_environment_overrides_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMEZONE", "Europe/Lisbon")
    monkeypatch.setenv("SEND_INTERVAL_DAYS", "3")
    monkeypatch.setenv("SEND_HOUR", "19")
    monkeypatch.setenv("SEND_MINUTE", "45")
    monkeypatch.setenv("EMAIL_TO", "a@example.com, b@example.com;c@example.com")
    config = load_config(load_dotenv_file=False)
    assert config.newsletter.timezone == "Europe/Lisbon"
    assert config.newsletter.frequency_days == 3
    assert config.newsletter.send_time == "19:45"
    assert config.email.recipients == ["a@example.com", "b@example.com", "c@example.com"]


def test_missing_api_key_falls_back_to_stub(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config = load_config(load_dotenv_file=False)
    assert config.llm.provider == "stub"
    assert config.llm_fell_back_to_stub is True


def test_api_key_present_keeps_provider(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    config = load_config(load_dotenv_file=False)
    assert config.llm.provider == "anthropic"
    assert config.llm_fell_back_to_stub is False


def test_secrets_are_excluded_from_dumps(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-value-12345")
    monkeypatch.setenv("SMTP_PASSWORD", "hunter2-password")
    config = load_config(load_dotenv_file=False)
    dumped = config.model_dump_json()
    assert "sk-secret-value-12345" not in dumped
    assert "hunter2-password" not in dumped


def test_bad_timezone_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus_Mons")
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        load_config(load_dotenv_file=False)


def test_bad_send_time_is_rejected(tmp_path):
    raw = yaml.safe_load((_repo() / "config" / "config.yaml").read_text())
    raw["newsletter"]["send_time"] = "25:99"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="send_time"):
        load_config(path, load_dotenv_file=False)


def test_music_must_be_a_category(tmp_path):
    raw = yaml.safe_load((_repo() / "config" / "config.yaml").read_text())
    raw["content"]["categories"] = ["history", "science"]
    path = tmp_path / "nomusic.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="first-class domain"):
        load_config(path, load_dotenv_file=False)


def test_funnel_must_narrow(tmp_path):
    raw = yaml.safe_load((_repo() / "config" / "config.yaml").read_text())
    raw["pipeline"]["triage_keep"] = 999
    path = tmp_path / "wide.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="narrow"):
        load_config(path, load_dotenv_file=False)


def test_unknown_config_key_is_rejected(tmp_path):
    raw = yaml.safe_load((_repo() / "config" / "config.yaml").read_text())
    raw["newsletter"]["frequncy_days"] = 2  # typo
    path = tmp_path / "typo.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        load_config(path, load_dotenv_file=False)


def test_music_taxonomy_is_extensible():
    config = load_config(load_dotenv_file=False)
    assert config.music.parent_genre("progressive_rock") == "rock"
    assert config.music.parent_genre("carnatic") == "indian_classical"
    assert config.music.parent_genre("not_a_genre") is None
    assert len(config.music.all_genres) > 80


def _repo():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent
