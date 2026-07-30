from types import SimpleNamespace

from miss_quote.bot.client import VoiceAction, humans_in, plan_voice_action

AUTOJOIN_ON = True
AUTOJOIN_OFF = False
NOT_CONNECTED = None


def _member(is_bot: bool = False):
    return SimpleNamespace(bot=is_bot)


def _channel(name: str, humans: int = 0, bots: int = 0):
    members = [_member() for _ in range(humans)] + [
        _member(is_bot=True) for _ in range(bots)
    ]
    return SimpleNamespace(name=name, members=members)


def _state(channel):
    return SimpleNamespace(channel=channel)


def test_autojoin_disabled_never_connects() -> None:
    general = _channel("general", humans=1)

    action, target = plan_voice_action(
        _state(None), _state(general), NOT_CONNECTED, AUTOJOIN_OFF
    )

    assert action is VoiceAction.NONE
    assert target is None


def test_autojoin_connects_when_a_human_arrives() -> None:
    general = _channel("general", humans=1)

    action, target = plan_voice_action(
        _state(None), _state(general), NOT_CONNECTED, AUTOJOIN_ON
    )

    assert action is VoiceAction.JOIN
    assert target is general


def test_already_connected_does_not_reconnect_to_the_same_channel() -> None:
    general = _channel("general", humans=2, bots=1)

    action, _ = plan_voice_action(
        _state(None), _state(general), general, AUTOJOIN_ON
    )

    assert action is VoiceAction.NONE


def test_stays_put_when_a_second_channel_becomes_active() -> None:
    """One voice channel per guild — hopping would fragment both transcripts."""
    general = _channel("general", humans=1, bots=1)
    gaming = _channel("gaming", humans=1)

    action, _ = plan_voice_action(
        _state(None), _state(gaming), general, AUTOJOIN_ON
    )

    assert action is VoiceAction.NONE


def test_leaves_when_the_channel_empties_of_humans() -> None:
    general = _channel("general", humans=0, bots=1)

    action, target = plan_voice_action(
        _state(general), _state(None), general, AUTOJOIN_ON
    )

    assert action is VoiceAction.LEAVE
    assert target is general


def test_stays_while_humans_remain() -> None:
    general = _channel("general", humans=1, bots=1)

    action, _ = plan_voice_action(
        _state(general), _state(None), general, AUTOJOIN_ON
    )

    assert action is VoiceAction.NONE


def test_mute_and_deafen_changes_are_ignored() -> None:
    """Same channel before and after means nothing about membership changed."""
    general = _channel("general", humans=1)

    action, _ = plan_voice_action(
        _state(general), _state(general), general, AUTOJOIN_ON
    )

    assert action is VoiceAction.NONE


def test_humans_in_excludes_bots() -> None:
    assert humans_in(_channel("general", humans=2, bots=3)) == 2
    assert humans_in(_channel("empty", humans=0, bots=1)) == 0
    assert humans_in(None) == 0
