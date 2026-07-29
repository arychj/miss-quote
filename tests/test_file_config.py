import importlib
from pathlib import Path

import pytest

import config as config_module

FIRST_SERVER = 123456789012345678
SECOND_SERVER = 876543210987654321
UNKNOWN_SERVER = 111222333444555666

KNOWN_USER = 234567890123456789
UNKNOWN_USER = 999888777
REPORTED_NAME = "xX_nickname_Xx"

TOOL = "example-tool"

FULL_CONFIG = f"""
servers:
  {FIRST_SERVER}:
    alias: first-server
    users:
      {KNOWN_USER}: Speaker One
    tools:
      {TOOL}:
        enabled: true
        config:
          some-setting: a value

  {SECOND_SERVER}:
    alias: second-server
    users:
      {KNOWN_USER}: Someone Else
"""


def _load(monkeypatch, tmp_path, body: str | None):
    """Load FileConfig against a temporary file, or none at all."""
    path = tmp_path / "config.yaml"
    if body is not None:
        path.write_text(body, encoding="utf-8")

    monkeypatch.setenv("CONFIG_FILE", str(path))
    reloaded = importlib.reload(config_module)
    return reloaded.FileConfig.load()


# ── servers and aliases ───────────────────────────


def test_servers_are_read_with_their_aliases(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, FULL_CONFIG)

    assert cfg.found is True
    assert cfg.knows(FIRST_SERVER)
    assert not cfg.knows(UNKNOWN_SERVER)
    assert cfg.alias_for(FIRST_SERVER) == "first-server"
    assert cfg.alias_for(UNKNOWN_SERVER) is None
    assert cfg.problems == ()


def test_missing_file_knows_nothing(monkeypatch, tmp_path):
    """Joining no server is recoverable; recording the wrong one is not."""
    cfg = _load(monkeypatch, tmp_path, body=None)

    assert cfg.found is False
    assert cfg.servers == {}
    assert not cfg.knows(FIRST_SERVER)


def test_empty_file_knows_nothing(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, body="")

    assert cfg.found is True
    assert not cfg.knows(FIRST_SERVER)


def test_absent_key_is_not_an_error(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, "servers:\n")

    assert cfg.servers == {}
    assert cfg.problems == ()


@pytest.mark.parametrize("quoted", ['"{id}"', "{id}"])
def test_ids_are_read_as_integers_however_they_are_written(
    monkeypatch, tmp_path, quoted: str
):
    """
    YAML quoting must not change behaviour.

    Discord IDs are long enough that quoting them is a natural instinct, and a
    string key would silently never match an int ID.
    """
    server = quoted.format(id=FIRST_SERVER)
    user = quoted.format(id=KNOWN_USER)

    cfg = _load(
        monkeypatch,
        tmp_path,
        f"servers:\n  {server}:\n    alias: first-server\n"
        f"    users:\n      {user}: Speaker One\n",
    )

    assert cfg.knows(FIRST_SERVER)
    assert cfg.name_for(FIRST_SERVER, KNOWN_USER, REPORTED_NAME) == "Speaker One"


# ── malformed entries ─────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        f"servers:\n  {FIRST_SERVER}: first-server\n",
        f"servers:\n  {FIRST_SERVER}:\n    users:\n      {KNOWN_USER}: Speaker One\n",
        f"servers:\n  {FIRST_SERVER}:\n    alias: '   '\n",
        f"servers:\n  {FIRST_SERVER}:\n    alias: []\n",
    ],
    ids=["bare string", "no alias", "blank alias", "alias is not a string"],
)
def test_a_server_without_an_alias_is_dropped_and_reported(monkeypatch, tmp_path, body):
    """A typo costs one server, reported at startup — not a crash-looping pod."""
    cfg = _load(monkeypatch, tmp_path, body)

    assert not cfg.knows(FIRST_SERVER)
    assert cfg.problems, "a dropped server must say why"


def test_a_key_that_is_not_an_id_is_dropped_and_reported(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, "servers:\n  first-server:\n    alias: first\n")

    assert cfg.servers == {}
    assert cfg.problems


def test_one_bad_server_does_not_take_the_others_with_it(monkeypatch, tmp_path):
    cfg = _load(
        monkeypatch,
        tmp_path,
        f"servers:\n  {FIRST_SERVER}:\n    alias: first-server\n"
        f"  {SECOND_SERVER}:\n    users: {{}}\n",
    )

    assert cfg.knows(FIRST_SERVER)
    assert not cfg.knows(SECOND_SERVER)
    assert len(cfg.problems) == 1


def test_a_name_under_a_non_id_is_dropped_without_losing_the_server(
    monkeypatch, tmp_path
):
    cfg = _load(
        monkeypatch,
        tmp_path,
        f"servers:\n  {FIRST_SERVER}:\n    alias: first-server\n"
        f"    users:\n      someone: Speaker One\n",
    )

    assert cfg.knows(FIRST_SERVER)
    assert cfg.servers[FIRST_SERVER].users == {}
    assert cfg.problems


# ── names ─────────────────────────────────────────


def test_names_are_scoped_to_their_server(monkeypatch, tmp_path):
    """The same person can be known differently in two servers."""
    cfg = _load(monkeypatch, tmp_path, FULL_CONFIG)

    assert cfg.name_for(FIRST_SERVER, KNOWN_USER, REPORTED_NAME) == "Speaker One"
    assert cfg.name_for(SECOND_SERVER, KNOWN_USER, REPORTED_NAME) == "Someone Else"


def test_unmapped_user_keeps_the_reported_name(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, FULL_CONFIG)

    assert cfg.name_for(FIRST_SERVER, UNKNOWN_USER, REPORTED_NAME) == REPORTED_NAME


def test_unknown_server_keeps_the_reported_name(monkeypatch, tmp_path):
    """No entry means no roster to look the speaker up in."""
    cfg = _load(monkeypatch, tmp_path, FULL_CONFIG)

    assert cfg.name_for(UNKNOWN_SERVER, KNOWN_USER, REPORTED_NAME) == REPORTED_NAME


def test_server_without_a_roster_keeps_reported_names(monkeypatch, tmp_path):
    cfg = _load(
        monkeypatch, tmp_path, f"servers:\n  {FIRST_SERVER}:\n    alias: first-server\n"
    )

    assert cfg.knows(FIRST_SERVER)
    assert cfg.name_for(FIRST_SERVER, KNOWN_USER, REPORTED_NAME) == REPORTED_NAME


def test_an_empty_roster_is_not_an_error(monkeypatch, tmp_path):
    cfg = _load(
        monkeypatch,
        tmp_path,
        f"servers:\n  {FIRST_SERVER}:\n    alias: first-server\n    users:\n",
    )

    assert cfg.name_for(FIRST_SERVER, KNOWN_USER, REPORTED_NAME) == REPORTED_NAME
    assert cfg.problems == ()


# ── tools ─────────────────────────────────────────


def test_tools_carry_their_settings(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, FULL_CONFIG)

    tools = cfg.tools_for(FIRST_SERVER)

    assert tools[TOOL].enabled is True
    assert tools[TOOL].config == {"some-setting": "a value"}


def test_a_server_with_no_tools_has_none(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, FULL_CONFIG)

    assert cfg.tools_for(SECOND_SERVER) == {}
    assert cfg.tools_for(UNKNOWN_SERVER) == {}


def test_a_tool_is_off_unless_it_says_otherwise(monkeypatch, tmp_path):
    """Enabling a tool is a decision, and it should have to be written down."""
    cfg = _load(
        monkeypatch,
        tmp_path,
        f"servers:\n  {FIRST_SERVER}:\n    alias: first-server\n"
        f"    tools:\n      {TOOL}:\n        config:\n          key: value\n",
    )

    assert cfg.tools_for(FIRST_SERVER)[TOOL].enabled is False


def test_a_tool_with_no_body_is_off_and_configless(monkeypatch, tmp_path):
    cfg = _load(
        monkeypatch,
        tmp_path,
        f"servers:\n  {FIRST_SERVER}:\n    alias: first-server\n"
        f"    tools:\n      {TOOL}:\n",
    )

    settings = cfg.tools_for(FIRST_SERVER)[TOOL]

    assert settings.enabled is False
    assert settings.config == {}


def test_a_tool_without_a_config_gets_an_empty_one(monkeypatch, tmp_path):
    cfg = _load(
        monkeypatch,
        tmp_path,
        f"servers:\n  {FIRST_SERVER}:\n    alias: first-server\n"
        f"    tools:\n      {TOOL}:\n        enabled: true\n",
    )

    assert cfg.tools_for(FIRST_SERVER)[TOOL].config == {}


def test_a_tool_whose_config_is_not_a_mapping_is_reported(monkeypatch, tmp_path):
    cfg = _load(
        monkeypatch,
        tmp_path,
        f"servers:\n  {FIRST_SERVER}:\n    alias: first-server\n"
        f"    tools:\n      {TOOL}:\n        enabled: true\n        config: nonsense\n",
    )

    assert cfg.tools_for(FIRST_SERVER)[TOOL].config == {}
    assert cfg.problems


def test_a_malformed_tool_does_not_cost_the_server(monkeypatch, tmp_path):
    cfg = _load(
        monkeypatch,
        tmp_path,
        f"servers:\n  {FIRST_SERVER}:\n    alias: first-server\n"
        f"    tools:\n      {TOOL}: nonsense\n",
    )

    assert cfg.knows(FIRST_SERVER)
    assert cfg.tools_for(FIRST_SERVER) == {}
    assert cfg.problems


# ── the shipped file ──────────────────────────────


def test_shipped_config_parses(monkeypatch, tmp_path):
    """The example in the repo is what gets copied into the ConfigMap."""
    shipped = Path(__file__).resolve().parent.parent / "config.yaml"
    cfg = _load(monkeypatch, tmp_path, shipped.read_text(encoding="utf-8"))

    assert cfg.servers
    assert cfg.problems == (), "the shipped example must not trip its own parser"
    assert all(isinstance(server, int) for server in cfg.servers)
    assert all(server.alias for server in cfg.servers.values())
