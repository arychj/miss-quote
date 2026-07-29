import importlib

import pytest


def test_config_reads_environment_values(monkeypatch) -> None:
    monkeypatch.setenv("COMMAND_PREFIX", "?")
    monkeypatch.setenv("STT_LANGUAGE", "de")
    monkeypatch.setenv("MAX_CONCURRENT_TRANSCRIPTIONS", "9")
    monkeypatch.setenv("WYOMING_HOST", "asr.internal")

    import config

    reloaded = importlib.reload(config)

    assert reloaded.discord_cfg.command_prefix == "?"
    assert reloaded.stt_cfg.language == "de"
    assert reloaded.stt_cfg.max_concurrent == 9
    assert reloaded.stt_cfg.host == "asr.internal"


def test_the_head_start_is_measured_in_playback_bytes(monkeypatch) -> None:
    """A duration is the only sane unit to configure; the player wants bytes."""
    monkeypatch.setenv("TTS_LEAD_MS", "500")

    import config

    reloaded = importlib.reload(config)
    playback = reloaded.audio_cfg
    half_a_second = (
        playback.playback_sample_rate
        * playback.playback_channels
        * playback.sample_width
        // 2
    )

    assert reloaded.tts_cfg.lead_bytes == half_a_second


def test_no_head_start_waits_for_nothing(monkeypatch) -> None:
    monkeypatch.setenv("TTS_LEAD_MS", "0")

    import config

    assert importlib.reload(config).tts_cfg.lead_bytes == 0


def test_invalid_integer_config_fails_fast(monkeypatch) -> None:
    import config

    monkeypatch.setenv("MAX_CONCURRENT_TRANSCRIPTIONS", "not-an-int")

    with pytest.raises(ValueError) as exc:
        config._env_int("MAX_CONCURRENT_TRANSCRIPTIONS", 1)

    assert "MAX_CONCURRENT_TRANSCRIPTIONS must be an integer" in str(exc.value)


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_truthy_booleans(monkeypatch, value: str) -> None:
    import config

    monkeypatch.setenv("AUTOJOIN", value)
    assert config._env_bool("AUTOJOIN", False) is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_falsey_booleans(monkeypatch, value: str) -> None:
    import config

    monkeypatch.setenv("AUTOJOIN", value)
    assert config._env_bool("AUTOJOIN", True) is False


def test_invalid_boolean_fails_fast(monkeypatch) -> None:
    import config

    monkeypatch.setenv("AUTOJOIN", "maybe")

    with pytest.raises(ValueError) as exc:
        config._env_bool("AUTOJOIN", True)

    assert "AUTOJOIN must be a boolean" in str(exc.value)


def test_autojoin_defaults_to_true(monkeypatch) -> None:
    monkeypatch.delenv("AUTOJOIN", raising=False)

    import config

    reloaded = importlib.reload(config)

    assert reloaded.discord_cfg.autojoin is True


def test_defaults_name_no_particular_deployment(monkeypatch) -> None:
    """
    Defaults must not encode a specific cluster. The ASR host is a deployment
    detail and belongs in the manifest, not baked into the image.
    """
    for name in ("WYOMING_HOST", "WYOMING_PORT", "TRANSCRIPT_DIR"):
        monkeypatch.delenv(name, raising=False)

    import config

    reloaded = importlib.reload(config)

    assert reloaded.stt_cfg.host == "localhost"
    assert reloaded.stt_cfg.port == 10300
    assert str(reloaded.transcript_cfg.directory) == "/transcripts"


def test_retention_defaults_to_keep_forever(monkeypatch) -> None:
    monkeypatch.delenv("RETENTION_DAYS", raising=False)

    import config

    reloaded = importlib.reload(config)

    assert reloaded.transcript_cfg.retention_days == -1
    assert reloaded.transcript_cfg.retention_enabled is False
