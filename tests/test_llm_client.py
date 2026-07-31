import json

import aiohttp
import pytest

import miss_quote.llm.client as client_module
from miss_quote.config import LLMConfig
from miss_quote.llm.client import CompletionError, complete

BASE_URL = "http://endpoint.invalid/v1"
MODEL = "a-model"
SECRET_KEY = "sk-do-not-print-me"

INSTRUCTION = "Summarize this."
TEXT = "Erik: that should work\nEli: it did not"
ANSWER = "They disagreed."

MAX_TOKENS = 1024
TEMPERATURE = 0.7
TIMEOUT_SECONDS = 30.0


class Endpoint:
    """A stand-in for the session, remembering what it was asked for."""

    def __init__(self, status: int = 200, body: str | None = None) -> None:
        self.status = status
        self.body = _completion(ANSWER) if body is None else body
        self.url: str | None = None
        self.payload: dict | None = None
        self.headers: dict | None = None
        self.closed = False

    def post(self, url, json=None, headers=None):
        self.url = url
        self.payload = json
        self.headers = headers

        return _Response(self.status, self.body)

    async def close(self) -> None:
        self.closed = True


class _Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def text(self) -> str:
        return self._body


class Unreachable:
    """A session whose every request fails the way a down endpoint does."""

    def post(self, *args, **kwargs):
        raise aiohttp.ClientConnectionError("connection refused")

    async def close(self) -> None:
        return None


def _completion(content: str) -> str:
    return json.dumps({"choices": [{"message": {"content": content}}]})


@pytest.fixture
def endpoint(monkeypatch):
    """An endpoint the client talks to instead of the network."""
    _configure(monkeypatch)
    served = Endpoint()
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    return served


def _configure(
    monkeypatch,
    api_key: str = SECRET_KEY,
    model: str = MODEL,
    base_url: str = BASE_URL,
) -> None:
    """
    Point the client at a made-up endpoint.

    The real config is a frozen dataclass built from the environment, so this
    replaces the whole of it rather than reaching into one field of it.
    """
    monkeypatch.setattr(
        client_module,
        "llm_cfg",
        LLMConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            timeout_seconds=TIMEOUT_SECONDS,
        ),
    )


async def test_the_completion_comes_back(endpoint):
    assert await complete(INSTRUCTION, TEXT) == ANSWER


async def test_the_request_is_a_chat_completion(endpoint):
    await complete(INSTRUCTION, TEXT)

    assert endpoint.url == f"{BASE_URL}/chat/completions"
    assert endpoint.payload["model"] == MODEL
    assert endpoint.payload["max_tokens"] == MAX_TOKENS
    assert endpoint.payload["temperature"] == TEMPERATURE
    assert endpoint.payload["messages"] == [
        {"role": "system", "content": INSTRUCTION},
        {"role": "user", "content": TEXT},
    ]


async def test_a_root_with_a_trailing_slash_is_the_same_endpoint(monkeypatch):
    _configure(monkeypatch, base_url=BASE_URL + "/")
    served = Endpoint()
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    await complete(INSTRUCTION, TEXT)

    assert served.url == f"{BASE_URL}/chat/completions"


async def test_a_key_is_sent_as_a_bearer_token(endpoint):
    await complete(INSTRUCTION, TEXT)

    assert endpoint.headers == {"Authorization": f"Bearer {SECRET_KEY}"}


async def test_no_key_sends_no_authorization_at_all(monkeypatch):
    """An endpoint that wants no credential is not handed an empty one."""
    _configure(monkeypatch, api_key="")
    served = Endpoint()
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    await complete(INSTRUCTION, TEXT)

    assert served.headers == {}


async def test_an_unconfigured_endpoint_says_so(monkeypatch):
    _configure(monkeypatch, model="")

    with pytest.raises(CompletionError) as raised:
        await complete(INSTRUCTION, TEXT)

    assert "LLM_MODEL" in str(raised.value)


async def test_a_refusal_carries_the_status_and_the_body(monkeypatch):
    _configure(monkeypatch)
    served = Endpoint(status=429, body="slow down")
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    with pytest.raises(CompletionError) as raised:
        await complete(INSTRUCTION, TEXT)

    assert "429" in str(raised.value)
    assert "slow down" in str(raised.value)


async def test_the_key_is_never_in_a_failure(monkeypatch):
    """A secret in an exception message is a secret in a log."""
    _configure(monkeypatch)

    for served in (Endpoint(status=401, body="unauthorized"), Unreachable()):
        monkeypatch.setattr(client_module, "_shared_session", lambda: served)

        with pytest.raises(CompletionError) as raised:
            await complete(INSTRUCTION, TEXT)

        assert SECRET_KEY not in str(raised.value)
        assert SECRET_KEY not in repr(raised.value)


async def test_a_body_that_is_not_a_completion_is_a_failure(monkeypatch):
    _configure(monkeypatch)
    served = Endpoint(body="<html>gateway timeout</html>")
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    with pytest.raises(CompletionError):
        await complete(INSTRUCTION, TEXT)


async def test_no_choices_is_a_failure_rather_than_an_empty_summary(monkeypatch):
    """An empty `choices` is what a filtered request looks like on several of them."""
    _configure(monkeypatch)
    served = Endpoint(body=json.dumps({"choices": []}))
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    with pytest.raises(CompletionError):
        await complete(INSTRUCTION, TEXT)


async def test_an_empty_completion_is_a_failure(monkeypatch):
    _configure(monkeypatch)
    served = Endpoint(body=_completion("   "))
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    with pytest.raises(CompletionError):
        await complete(INSTRUCTION, TEXT)


async def test_an_unreachable_endpoint_is_a_failure(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(client_module, "_shared_session", Unreachable)

    with pytest.raises(CompletionError):
        await complete(INSTRUCTION, TEXT)


async def test_closing_without_ever_asking_anything_is_a_no_op(monkeypatch):
    monkeypatch.setattr(client_module, "_session", None)

    await client_module.close()

    assert client_module._session is None
