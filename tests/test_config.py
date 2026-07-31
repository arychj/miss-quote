import importlib

import pytest

SETTINGS_FILE = "config.yaml"


def _reload_with(monkeypatch, tmp_path, body: str):
    """Reload the config module against a settings file of our own."""
    import miss_quote.config as config

    path = tmp_path / SETTINGS_FILE
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(path))

    return importlib.reload(config)


def _reload_with_setting(monkeypatch, tmp_path, section: str, key: str, value):
    return _reload_with(
        monkeypatch, tmp_path, f"settings:\n  {section}:\n    {key}: {value}\n"
    )


def _reload_without_settings(monkeypatch, tmp_path):
    """A file that says nothing, which is what every default has to survive."""
    import miss_quote.config as config

    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / SETTINGS_FILE))

    return importlib.reload(config)


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


def test_the_head_start_is_measured_in_playback_bytes(monkeypatch, tmp_path) -> None:
    """A duration is the only sane unit to configure; the player wants bytes."""
    reloaded = _reload_with_setting(monkeypatch, tmp_path, "tts", "lead_ms", 500)
    playback = reloaded.audio_cfg
    half_a_second = (
        playback.playback_sample_rate
        * playback.playback_channels
        * playback.sample_width
        // 2
    )

    assert reloaded.tts_cfg.lead_bytes == half_a_second


def test_no_head_start_waits_for_nothing(monkeypatch, tmp_path) -> None:
    reloaded = _reload_with_setting(monkeypatch, tmp_path, "tts", "lead_ms", 0)

    assert reloaded.tts_cfg.lead_bytes == 0


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


def test_the_volume_floor_is_read_as_a_fraction(monkeypatch, tmp_path) -> None:
    reloaded = _reload_with_setting(monkeypatch, tmp_path, "fines", "volume_floor", 0.4)

    assert reloaded.morality_cfg.volume_floor == 0.4


def test_a_volume_floor_of_zero_silences_a_repeat_offender(monkeypatch, tmp_path):
    reloaded = _reload_with_setting(monkeypatch, tmp_path, "fines", "volume_floor", 0)

    assert reloaded.morality_cfg.volume_floor == reloaded.SILENT_VOLUME


def test_a_volume_floor_above_unity_is_no_backoff_rather_than_a_boost(
    monkeypatch, tmp_path
) -> None:
    """There is nowhere to back off to; it must not become a way to get louder."""
    reloaded = _reload_with_setting(monkeypatch, tmp_path, "fines", "volume_floor", 4)

    assert reloaded.morality_cfg.volume_floor == reloaded.UNITY_VOLUME


def test_a_negative_volume_floor_is_silence_rather_than_an_inversion(
    monkeypatch, tmp_path
) -> None:
    reloaded = _reload_with_setting(monkeypatch, tmp_path, "fines", "volume_floor", -2)

    assert reloaded.morality_cfg.volume_floor == reloaded.SILENT_VOLUME


def test_the_backoff_step_is_read_as_a_percentage(monkeypatch, tmp_path) -> None:
    """A percentage is what somebody writes; a fraction is what scales audio."""
    reloaded = _reload_with_setting(
        monkeypatch, tmp_path, "fines", "backoff_percent", 20
    )

    assert reloaded.morality_cfg.backoff_step == 0.2


def test_a_backoff_step_of_zero_leaves_a_repeat_offender_at_full_volume(
    monkeypatch, tmp_path
) -> None:
    """Nothing comes off per violation, which is how the backoff is turned off."""
    reloaded = _reload_with_setting(
        monkeypatch, tmp_path, "fines", "backoff_percent", 0
    )

    assert reloaded.morality_cfg.backoff_step == reloaded.SILENT_VOLUME


def test_a_negative_backoff_step_does_not_make_a_repeat_offender_louder(
    monkeypatch, tmp_path
) -> None:
    reloaded = _reload_with_setting(
        monkeypatch, tmp_path, "fines", "backoff_percent", -10
    )

    assert reloaded.morality_cfg.backoff_step == reloaded.SILENT_VOLUME


def test_a_backoff_step_above_everything_reaches_the_floor_in_one(
    monkeypatch, tmp_path
) -> None:
    reloaded = _reload_with_setting(
        monkeypatch, tmp_path, "fines", "backoff_percent", 400
    )

    assert reloaded.morality_cfg.backoff_step == reloaded.UNITY_VOLUME


def test_the_backoff_window_is_read_in_seconds(monkeypatch, tmp_path) -> None:
    reloaded = _reload_with_setting(
        monkeypatch, tmp_path, "fines", "backoff_seconds", 45
    )

    assert reloaded.morality_cfg.backoff_seconds == 45.0


def test_the_currency_defaults_to_credits(monkeypatch, tmp_path) -> None:
    reloaded = _reload_without_settings(monkeypatch, tmp_path)

    assert reloaded.scoreboard_cfg.currency == "credit"


def test_the_currency_can_be_something_else(monkeypatch, tmp_path) -> None:
    reloaded = _reload_with_setting(
        monkeypatch, tmp_path, "credits", "currency", "buck"
    )

    assert reloaded.scoreboard_cfg.currency == "buck"


def test_the_topic_is_published_less_often_than_the_tally_is_saved() -> None:
    """A topic edit is rate limited to a couple per ten minutes; a write is not."""
    import miss_quote.config as config

    scoreboard = config.scoreboard_cfg

    assert scoreboard.topic_interval_seconds > scoreboard.save_interval_seconds


def test_publishing_stops_at_a_topic_interval_of_zero(monkeypatch, tmp_path) -> None:
    """So a deployment can keep the tally without touching a channel topic."""
    reloaded = _reload_with_setting(
        monkeypatch, tmp_path, "credits", "topic_seconds", 0
    )

    assert reloaded.scoreboard_cfg.topic_interval_seconds == 0
    assert reloaded.scoreboard_cfg.save_interval_seconds > 0


def test_counting_stops_at_a_save_interval_of_zero(monkeypatch, tmp_path) -> None:
    """Which leaves the tally in memory until shutdown writes it."""
    reloaded = _reload_with_setting(monkeypatch, tmp_path, "credits", "save_seconds", 0)

    assert reloaded.scoreboard_cfg.save_interval_seconds == 0


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


def test_retention_defaults_to_keep_forever(monkeypatch, tmp_path) -> None:
    reloaded = _reload_without_settings(monkeypatch, tmp_path)

    assert reloaded.transcript_cfg.retention_days == -1
    assert reloaded.transcript_cfg.retention_enabled is False


def test_a_setting_the_file_does_not_mention_keeps_its_default(
    monkeypatch, tmp_path
) -> None:
    """Nothing in the block is required; saying one thing must not reset the rest."""
    reloaded = _reload_with_setting(monkeypatch, tmp_path, "tts", "stall_seconds", 3)

    assert reloaded.tts_cfg.stall_seconds == 3.0
    assert reloaded.tts_cfg.timeout_seconds == 30.0
    assert reloaded.tts_cfg.cache_retention_days == 90


def test_an_unreadable_setting_falls_back_rather_than_stopping_the_pod(
    monkeypatch, tmp_path
) -> None:
    """The same file decides which servers are joined; a typo must not cost that."""
    reloaded = _reload_with_setting(
        monkeypatch, tmp_path, "quotes", "backoff_seconds", "'not a number'"
    )

    assert reloaded.quotes_cfg.backoff_seconds == 300.0
    assert reloaded.file_cfg.problems


def test_the_endpoint_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_BASE", "http://gateway.internal/v1")
    monkeypatch.setenv("LLM_MODEL", "a-model")
    monkeypatch.setenv("SUMMARY_DIR", "/somewhere/else")

    import miss_quote.config as config

    reloaded = importlib.reload(config)

    assert reloaded.llm_cfg.base_url == "http://gateway.internal/v1"
    assert reloaded.llm_cfg.model == "a-model"
    assert reloaded.llm_cfg.configured
    assert str(reloaded.summary_cfg.directory) == "/somewhere/else"


def test_an_endpoint_without_a_model_is_not_configured(monkeypatch) -> None:
    """A model name is a deployment's own; guessing one is a 404 that reads wrong."""
    monkeypatch.setenv("LLM_API_BASE", "http://gateway.internal/v1")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    import miss_quote.config as config

    assert not importlib.reload(config).llm_cfg.configured


def test_the_completion_budget_and_the_summary_retention_are_settings(
    monkeypatch, tmp_path
) -> None:
    reloaded = _reload_with(
        monkeypatch,
        tmp_path,
        "settings:\n"
        "  llm:\n    timeout_seconds: 45\n    max_tokens: 256\n"
        "  summaries:\n    retention_days: 365\n",
    )

    assert reloaded.llm_cfg.timeout_seconds == 45.0
    assert reloaded.llm_cfg.max_tokens == 256
    assert reloaded.llm_cfg.temperature == 0.7
    assert reloaded.summary_cfg.retention_days == 365
    assert reloaded.summary_cfg.retention_enabled


def test_summaries_are_kept_forever_unless_a_window_is_set(monkeypatch, tmp_path) -> None:
    """Any value below 1 keeps them, so a mis-set setting cannot destroy the archive."""
    reloaded = _reload_without_settings(monkeypatch, tmp_path)

    assert reloaded.summary_cfg.retention_days == -1
    assert not reloaded.summary_cfg.retention_enabled
