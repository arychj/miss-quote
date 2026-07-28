import importlib

import pytest

import config as config_module

ALLOWED_SERVER = 123456789012345678
OTHER_SERVER = 111222333444555666
KNOWN_USER = 234567890123456789
UNKNOWN_USER = 999888777
REPORTED_NAME = "xX_nickname_Xx"
CONFIGURED_NAME = "Speaker One"


def _load(monkeypatch, tmp_path, body: str | None):
    """Load FileConfig against a temporary file, or none at all."""
    path = tmp_path / "config.yaml"
    if body is not None:
        path.write_text(body, encoding="utf-8")

    monkeypatch.setenv("CONFIG_FILE", str(path))
    reloaded = importlib.reload(config_module)
    return reloaded.FileConfig.load()


def test_allowlist_and_names_are_read(monkeypatch, tmp_path):
    cfg = _load(
        monkeypatch,
        tmp_path,
        f"""
allowed_servers:
  - {ALLOWED_SERVER}
user_names:
  {KNOWN_USER}: {CONFIGURED_NAME}
""",
    )

    assert cfg.found is True
    assert cfg.allows(ALLOWED_SERVER)
    assert not cfg.allows(OTHER_SERVER)
    assert cfg.name_for(KNOWN_USER, REPORTED_NAME) == CONFIGURED_NAME
    assert cfg.name_for(UNKNOWN_USER, REPORTED_NAME) == REPORTED_NAME


def test_missing_file_allows_nothing(monkeypatch, tmp_path):
    """Joining no server is recoverable; recording the wrong one is not."""
    cfg = _load(monkeypatch, tmp_path, body=None)

    assert cfg.found is False
    assert cfg.allowed_servers == frozenset()
    assert not cfg.allows(ALLOWED_SERVER)


def test_empty_file_allows_nothing(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, body="")

    assert cfg.found is True
    assert not cfg.allows(ALLOWED_SERVER)


def test_absent_keys_are_not_an_error(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, "allowed_servers:\nuser_names:\n")

    assert cfg.allowed_servers == frozenset()
    assert cfg.name_for(KNOWN_USER, REPORTED_NAME) == REPORTED_NAME


@pytest.mark.parametrize("quoted", ['"{id}"', "{id}"])
def test_ids_are_read_as_integers_however_they_are_written(
    monkeypatch, tmp_path, quoted: str
):
    """
    YAML quoting must not change behaviour.

    Discord IDs are long enough that quoting them is a natural instinct, and a
    string key would silently never match an int user ID.
    """
    server = quoted.format(id=ALLOWED_SERVER)
    user = quoted.format(id=KNOWN_USER)

    cfg = _load(
        monkeypatch,
        tmp_path,
        f"allowed_servers:\n  - {server}\nuser_names:\n  {user}: {CONFIGURED_NAME}\n",
    )

    assert cfg.allows(ALLOWED_SERVER)
    assert cfg.name_for(KNOWN_USER, REPORTED_NAME) == CONFIGURED_NAME


def test_shipped_config_parses(monkeypatch, tmp_path):
    """The example in the repo is what gets copied into the ConfigMap."""
    from pathlib import Path

    shipped = Path(__file__).resolve().parent.parent / "config.yaml"
    cfg = _load(monkeypatch, tmp_path, shipped.read_text(encoding="utf-8"))

    assert cfg.allowed_servers
    assert all(isinstance(server, int) for server in cfg.allowed_servers)
    assert all(isinstance(user, int) for user in cfg.user_names)
