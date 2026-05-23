import importlib

import pytest


def test_config_reads_environment_values(monkeypatch) -> None:
    monkeypatch.setenv("COMMAND_PREFIX", "?")
    monkeypatch.setenv("STT_LANGUAGE", "en")
    monkeypatch.setenv("AUDIO_QUEUE_MAXSIZE", "42")

    import config

    reloaded = importlib.reload(config)

    assert reloaded.discord_cfg.command_prefix == "?"
    assert reloaded.stt_cfg.language == "en"
    assert reloaded.process_cfg.audio_queue_maxsize == 42


def test_invalid_integer_config_fails_fast(monkeypatch) -> None:
    monkeypatch.setenv("AUDIO_QUEUE_MAXSIZE", "not-an-int")

    import config

    with pytest.raises(ValueError) as exc:
        config._env_int("AUDIO_QUEUE_MAXSIZE", 1)

    assert "AUDIO_QUEUE_MAXSIZE must be an integer" in str(exc.value)
