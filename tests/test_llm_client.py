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

MAX_OUTPUT_TOKENS = 1024
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
    thinking: bool = True,
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
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
            timeout_seconds=TIMEOUT_SECONDS,
            thinking=thinking,
        ),
    )


async def test_the_completion_comes_back(endpoint):
    assert await complete(INSTRUCTION, TEXT) == ANSWER


async def test_the_request_is_a_chat_completion(endpoint):
    await complete(INSTRUCTION, TEXT)

    assert endpoint.url == f"{BASE_URL}/chat/completions"
    assert endpoint.payload["model"] == MODEL
    assert endpoint.payload["max_tokens"] == MAX_OUTPUT_TOKENS
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


async def test_thinking_is_left_alone_unless_it_is_turned_off(endpoint):
    """
    An endpoint that has never heard of the field is one this never mentions it
    to, which is what keeps the request ordinary for a model that never reasons.
    """
    await complete(INSTRUCTION, TEXT)

    assert "chat_template_kwargs" not in endpoint.payload


async def test_turning_thinking_off_says_so_in_the_body(monkeypatch):
    _configure(monkeypatch, thinking=False)
    served = Endpoint()
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    await complete(INSTRUCTION, TEXT)

    assert served.payload["chat_template_kwargs"] == {"enable_thinking": False}


async def test_a_budget_spent_entirely_on_reasoning_says_which_setting(monkeypatch):
    """
    The failure that cost a debugging session: a 200, an empty content, and no
    hint that the number in the config file is what did it.
    """
    _configure(monkeypatch)
    served = Endpoint(
        body=json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "", "reasoning_content": "Let me think"},
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    with pytest.raises(CompletionError) as raised:
        await complete(INSTRUCTION, TEXT)

    said = str(raised.value)
    assert "reasoning" in said
    assert "max_output_tokens" in said
    assert "thinking" in said


async def test_an_answer_cut_off_without_reasoning_says_to_raise_the_budget(monkeypatch):
    _configure(monkeypatch)
    served = Endpoint(
        body=json.dumps(
            {"choices": [{"finish_reason": "length", "message": {"content": "   "}}]}
        )
    )
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    with pytest.raises(CompletionError, match="max_output_tokens"):
        await complete(INSTRUCTION, TEXT)


async def test_a_null_content_is_not_read_as_the_string_none(monkeypatch):
    """Several endpoints send `content: null` beside a reasoning block."""
    _configure(monkeypatch)
    served = Endpoint(body=json.dumps({"choices": [{"message": {"content": None}}]}))
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    with pytest.raises(CompletionError):
        await complete(INSTRUCTION, TEXT)


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


# ── reasoning models ──────────────────────────


async def test_inline_thinking_is_cut_off_the_front(monkeypatch):
    """
    Some stacks fence the thinking into the answer instead of a second field.
    Left in, the summary opens with the model talking to itself and the
    synthesizer reads the tags out loud.
    """
    _configure(monkeypatch)
    served = Endpoint(
        body=_completion(
            "<think>Let me work through who said what.</think>\n\nThey disagreed."
        )
    )
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    assert await complete(INSTRUCTION, TEXT) == "They disagreed."


@pytest.mark.parametrize(
    "fenced",
    [
        "<thinking>hmm</thinking>They disagreed.",
        "<reasoning>hmm</reasoning>\nThey disagreed.",
        "<thought>hmm</thought> They disagreed.",
        "<THINK>hmm</THINK>They disagreed.",
        "<think attr='1'>hmm</think>They disagreed.",
        "<think>one</think><think>two</think>They disagreed.",
        "<think>over\nseveral\nlines</think>\n\nThey disagreed.",
    ],
)
async def test_the_spellings_a_stack_might_use_are_all_cut(monkeypatch, fenced):
    _configure(monkeypatch)
    served = Endpoint(body=_completion(fenced))
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    assert await complete(INSTRUCTION, TEXT) == "They disagreed."


async def test_an_unclosed_thought_takes_the_rest_with_it(monkeypatch):
    """A model cut off mid-thought leaves an opening tag and no partner."""
    _configure(monkeypatch)
    served = Endpoint(body=_completion("<think>I am still thinking about"))
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    with pytest.raises(CompletionError, match="reasoning"):
        await complete(INSTRUCTION, TEXT)


async def test_an_answer_that_merely_mentions_thinking_survives(monkeypatch):
    """The tag is a tag, not the word."""
    _configure(monkeypatch)
    said = "They spent the evening thinking about <b>tags</b> and got nowhere."
    served = Endpoint(body=_completion(said))
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    assert await complete(INSTRUCTION, TEXT) == said


async def test_thinking_is_stripped_even_when_it_was_asked_to_be_off(monkeypatch):
    """The setting is a request, not a guarantee; an endpoint may ignore it."""
    _configure(monkeypatch, thinking=False)
    served = Endpoint(body=_completion("<think>anyway</think>They disagreed."))
    monkeypatch.setattr(client_module, "_shared_session", lambda: served)

    assert await complete(INSTRUCTION, TEXT) == "They disagreed."
