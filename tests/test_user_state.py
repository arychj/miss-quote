import time
from types import SimpleNamespace

import miss_quote.stt.user_state as user_state


def test_stale_speech_state_is_detected(monkeypatch) -> None:
    monkeypatch.setattr(
        user_state,
        "process_cfg",
        SimpleNamespace(speech_flush_timeout_seconds=1.0, user_timeout_seconds=60),
    )
    manager = user_state.UserStateManager(vad_iterator_factory=object)
    state = manager.get_or_create(123)
    state.speech_buffer.extend(b"speech")
    state.last_activity = time.time() - 2

    assert manager.stale_speech_states() == [state]


def test_cleanup_inactive_returns_removed_states(monkeypatch) -> None:
    monkeypatch.setattr(
        user_state,
        "process_cfg",
        SimpleNamespace(speech_flush_timeout_seconds=1.0, user_timeout_seconds=1),
    )
    manager = user_state.UserStateManager(vad_iterator_factory=object)
    state = manager.get_or_create(456)
    state.last_activity = time.time() - 2

    removed = manager.cleanup_inactive()

    assert removed == [state]
    assert manager.active_count == 0
