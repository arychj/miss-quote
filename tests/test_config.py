import importlib

import pytest


def test_config_reads_environment_values(monkeypatch) -> None:
    monkeypatch.setenv("COMMAND_PREFIX", "?")
    monkeypatch.setenv("STT_LANGUAGE", "de")
    monkeypatch.setenv("MAX_CONCURRENT_TRANSCRIPTIONS", "9")
    monkeypatch.setenv("WYOMING_HOST", "asr.internal")

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.discord_cfg.command_prefix == "?"
    assert reloaded.stt_cfg.language == "de"
    assert reloaded.stt_cfg.max_concurrent == 9
    assert reloaded.stt_cfg.host == "asr.internal"


def test_the_head_start_is_measured_in_playback_bytes(monkeypatch) -> None:
    """A duration is the only sane unit to configure; the player wants bytes."""
    monkeypatch.setenv("TTS_LEAD_MS", "500")

    import miss_quote.config as config

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

    import miss_quote.config as config

    assert importlib.reload(config).tts_cfg.lead_bytes == 0


def test_the_playback_volume_is_read_as_a_scale(monkeypatch) -> None:
    monkeypatch.setenv("PLAYBACK_VOLUME", "0.8")

    import miss_quote.config as config

    assert importlib.reload(config).audio_cfg.playback_volume == 0.8


def test_a_negative_playback_volume_is_silence_rather_than_an_inversion(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PLAYBACK_VOLUME", "-1")

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.audio_cfg.playback_volume == reloaded.SILENT_VOLUME


def test_the_volume_floor_is_read_as_a_fraction(monkeypatch) -> None:
    monkeypatch.setenv("VIOLATION_VOLUME_FLOOR", "0.4")

    import miss_quote.config as config

    assert importlib.reload(config).morality_cfg.volume_floor == 0.4


def test_a_volume_floor_of_zero_silences_a_repeat_offender(monkeypatch) -> None:
    monkeypatch.setenv("VIOLATION_VOLUME_FLOOR", "0")

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.morality_cfg.volume_floor == reloaded.SILENT_VOLUME


def test_a_volume_floor_above_unity_is_no_backoff_rather_than_a_boost(
    monkeypatch,
) -> None:
    """There is nowhere to back off to; it must not become a way to get louder."""
    monkeypatch.setenv("VIOLATION_VOLUME_FLOOR", "4")

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.morality_cfg.volume_floor == reloaded.UNITY_VOLUME


def test_a_negative_volume_floor_is_silence_rather_than_an_inversion(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VIOLATION_VOLUME_FLOOR", "-2")

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.morality_cfg.volume_floor == reloaded.SILENT_VOLUME


def test_the_backoff_step_is_read_as_a_percentage(monkeypatch) -> None:
    """A percentage is what somebody writes; a fraction is what scales audio."""
    monkeypatch.setenv("VOLUME_BACKOFF_PERCENT", "20")

    import miss_quote.config as config

    assert importlib.reload(config).morality_cfg.backoff_step == 0.2


def test_a_backoff_step_of_zero_leaves_a_repeat_offender_at_full_volume(
    monkeypatch,
) -> None:
    """Nothing comes off per violation, which is how the backoff is turned off."""
    monkeypatch.setenv("VOLUME_BACKOFF_PERCENT", "0")

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.morality_cfg.backoff_step == reloaded.SILENT_VOLUME


def test_a_negative_backoff_step_does_not_make_a_repeat_offender_louder(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VOLUME_BACKOFF_PERCENT", "-10")

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.morality_cfg.backoff_step == reloaded.SILENT_VOLUME


def test_a_backoff_step_above_everything_reaches_the_floor_in_one(monkeypatch) -> None:
    monkeypatch.setenv("VOLUME_BACKOFF_PERCENT", "400")

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.morality_cfg.backoff_step == reloaded.UNITY_VOLUME


def test_the_backoff_window_is_read_in_seconds(monkeypatch) -> None:
    monkeypatch.setenv("VOLUME_BACKOFF_DURATION", "45")

    import miss_quote.config as config

    assert importlib.reload(config).morality_cfg.backoff_seconds == 45.0


def test_the_currency_defaults_to_credits(monkeypatch) -> None:
    monkeypatch.delenv("CREDIT_CURRENCY", raising=False)

    import miss_quote.config as config

    assert importlib.reload(config).scoreboard_cfg.currency == "credit"


def test_the_currency_can_be_something_else(monkeypatch) -> None:
    monkeypatch.setenv("CREDIT_CURRENCY", "buck")

    import miss_quote.config as config

    assert importlib.reload(config).scoreboard_cfg.currency == "buck"


def test_the_topic_is_published_less_often_than_the_tally_is_saved() -> None:
    """A topic edit is rate limited to a couple per ten minutes; a write is not."""
    import miss_quote.config as config

    scoreboard = config.scoreboard_cfg

    assert scoreboard.topic_interval_seconds > scoreboard.save_interval_seconds


def test_publishing_stops_at_a_topic_interval_of_zero(monkeypatch) -> None:
    """So a deployment can keep the tally without touching a channel topic."""
    monkeypatch.setenv("CREDITS_TOPIC_SECONDS", "0")

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.scoreboard_cfg.topic_interval_seconds == 0
    assert reloaded.scoreboard_cfg.save_interval_seconds > 0


def test_counting_stops_at_a_save_interval_of_zero(monkeypatch) -> None:
    """Which leaves the tally in memory until shutdown writes it."""
    monkeypatch.setenv("CREDITS_SAVE_SECONDS", "0")

    import miss_quote.config as config

    assert importlib.reload(config).scoreboard_cfg.save_interval_seconds == 0


def test_invalid_integer_config_fails_fast(monkeypatch) -> None:
    import miss_quote.config as config

    monkeypatch.setenv("MAX_CONCURRENT_TRANSCRIPTIONS", "not-an-int")

    with pytest.raises(ValueError) as exc:
        config._env_int("MAX_CONCURRENT_TRANSCRIPTIONS", 1)

    assert "MAX_CONCURRENT_TRANSCRIPTIONS must be an integer" in str(exc.value)


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_truthy_booleans(monkeypatch, value: str) -> None:
    import miss_quote.config as config

    monkeypatch.setenv("AUTOJOIN", value)
    assert config._env_bool("AUTOJOIN", False) is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_falsey_booleans(monkeypatch, value: str) -> None:
    import miss_quote.config as config

    monkeypatch.setenv("AUTOJOIN", value)
    assert config._env_bool("AUTOJOIN", True) is False


def test_invalid_boolean_fails_fast(monkeypatch) -> None:
    import miss_quote.config as config

    monkeypatch.setenv("AUTOJOIN", "maybe")

    with pytest.raises(ValueError) as exc:
        config._env_bool("AUTOJOIN", True)

    assert "AUTOJOIN must be a boolean" in str(exc.value)


def test_autojoin_defaults_to_true(monkeypatch) -> None:
    monkeypatch.delenv("AUTOJOIN", raising=False)

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.discord_cfg.autojoin is True


def test_defaults_name_no_particular_deployment(monkeypatch) -> None:
    """
    Defaults must not encode a specific cluster. The ASR host is a deployment
    detail and belongs in the manifest, not baked into the image.
    """
    for name in ("WYOMING_HOST", "WYOMING_PORT", "TRANSCRIPT_DIR"):
        monkeypatch.delenv(name, raising=False)

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.stt_cfg.host == "localhost"
    assert reloaded.stt_cfg.port == 10300
    assert str(reloaded.transcript_cfg.directory) == "/transcripts"


def test_retention_defaults_to_keep_forever(monkeypatch) -> None:
    monkeypatch.delenv("RETENTION_DAYS", raising=False)

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.transcript_cfg.retention_days == -1
    assert reloaded.transcript_cfg.retention_enabled is False
