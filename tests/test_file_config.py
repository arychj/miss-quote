import importlib

import pytest

import config as config_module

FIRST_SERVER = 123456789012345678
SECOND_SERVER = 876543210987654321
UNKNOWN_SERVER = 111222333444555666

KNOWN_USER = 234567890123456789
UNKNOWN_USER = 999888777
REPORTED_NAME = "xX_nickname_Xx"

FULL_CONFIG = f"""
known_servers:
  {FIRST_SERVER}: first-server
  {SECOND_SERVER}: second-server

user_names:
  first-server:
    {KNOWN_USER}: Speaker One
  second-server:
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


def test_known_servers_are_read_with_their_aliases(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, FULL_CONFIG)

    assert cfg.found is True
    assert cfg.knows(FIRST_SERVER)
    assert not cfg.knows(UNKNOWN_SERVER)
    assert cfg.alias_for(FIRST_SERVER) == "first-server"
    assert cfg.alias_for(UNKNOWN_SERVER) is None


def test_names_are_scoped_to_their_server(monkeypatch, tmp_path):
    """The same person can be known differently in two servers."""
    cfg = _load(monkeypatch, tmp_path, FULL_CONFIG)

    assert cfg.name_for(FIRST_SERVER, KNOWN_USER, REPORTED_NAME) == "Speaker One"
    assert cfg.name_for(SECOND_SERVER, KNOWN_USER, REPORTED_NAME) == "Someone Else"


def test_unmapped_user_keeps_the_reported_name(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, FULL_CONFIG)

    assert cfg.name_for(FIRST_SERVER, UNKNOWN_USER, REPORTED_NAME) == REPORTED_NAME


def test_unknown_server_keeps_the_reported_name(monkeypatch, tmp_path):
    """No alias means no roster to look the speaker up in."""
    cfg = _load(monkeypatch, tmp_path, FULL_CONFIG)

    assert cfg.name_for(UNKNOWN_SERVER, KNOWN_USER, REPORTED_NAME) == REPORTED_NAME


def test_server_without_a_roster_keeps_reported_names(monkeypatch, tmp_path):
    cfg = _load(
        monkeypatch, tmp_path, f"known_servers:\n  {FIRST_SERVER}: first-server\n"
    )

    assert cfg.knows(FIRST_SERVER)
    assert cfg.name_for(FIRST_SERVER, KNOWN_USER, REPORTED_NAME) == REPORTED_NAME


def test_missing_file_knows_nothing(monkeypatch, tmp_path):
    """Joining no server is recoverable; recording the wrong one is not."""
    cfg = _load(monkeypatch, tmp_path, body=None)

    assert cfg.found is False
    assert cfg.known_servers == {}
    assert not cfg.knows(FIRST_SERVER)


def test_empty_file_knows_nothing(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, body="")

    assert cfg.found is True
    assert not cfg.knows(FIRST_SERVER)


def test_absent_keys_are_not_an_error(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, "known_servers:\nuser_names:\n")

    assert cfg.known_servers == {}
    assert cfg.name_for(FIRST_SERVER, KNOWN_USER, REPORTED_NAME) == REPORTED_NAME


def test_a_server_listed_with_no_names_is_not_an_error(monkeypatch, tmp_path):
    cfg = _load(
        monkeypatch,
        tmp_path,
        f"known_servers:\n  {FIRST_SERVER}: first-server\nuser_names:\n  first-server:\n",
    )

    assert cfg.name_for(FIRST_SERVER, KNOWN_USER, REPORTED_NAME) == REPORTED_NAME


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
        f"known_servers:\n  {server}: first-server\n"
        f"user_names:\n  first-server:\n    {user}: Speaker One\n",
    )

    assert cfg.knows(FIRST_SERVER)
    assert cfg.name_for(FIRST_SERVER, KNOWN_USER, REPORTED_NAME) == "Speaker One"


def test_shipped_config_parses(monkeypatch, tmp_path):
    """The example in the repo is what gets copied into the ConfigMap."""
    from pathlib import Path

    shipped = Path(__file__).resolve().parent.parent / "config.yaml"
    cfg = _load(monkeypatch, tmp_path, shipped.read_text(encoding="utf-8"))

    assert cfg.known_servers
    assert all(isinstance(server, int) for server in cfg.known_servers)
    assert all(isinstance(alias, str) for alias in cfg.known_servers.values())

    for alias in cfg.user_names:
        assert alias in cfg.known_servers.values(), f"{alias} names no known server"
