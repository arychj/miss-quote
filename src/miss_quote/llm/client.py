"""
An OpenAI-compatible chat-completions client.

The third thing this process talks to over a network, after the two Wyoming
servers, and deliberately the least opinionated of them: a root, an optional
bearer token, and a model name. `/chat/completions` is the whole of the API
surface used, which is the part every endpoint claiming compatibility actually
implements, so a hosted API, a gateway in front of one, and a model on the next
machine over are the same three settings.

Unlike the two Wyoming clients this holds a connection pool, because it is HTTP
and a new TCP and TLS handshake per request is most of the cost of a short one.
The session is made on first use and belongs to whoever calls `close` — the
`summary` tool, from its own `close`.

Failure raises, on the same terms as `tts.client`: the runner isolates every
tool, so an exception here costs the summary and nothing else, and swallowing it
would leave a caller unable to tell an empty conversation from a broken endpoint.

**The key is never in an exception, a log line, or a repr.** A failure names the
status and what the body said, both of which are the endpoint's words rather
than the deployment's secret.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import aiohttp

from miss_quote.config import (
    LLM_SECTION,
    MAX_OUTPUT_TOKENS_KEY,
    SETTINGS_KEY,
    THINKING_KEY,
    llm_cfg,
)
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

COMPLETIONS_PATH = "/chat/completions"
PATH_SEPARATOR = "/"

SYSTEM_ROLE = "system"
USER_ROLE = "user"

MODEL_FIELD = "model"
MESSAGES_FIELD = "messages"
ROLE_FIELD = "role"
CONTENT_FIELD = "content"
MAX_TOKENS_FIELD = "max_tokens"
TEMPERATURE_FIELD = "temperature"

CHOICES_FIELD = "choices"
MESSAGE_FIELD = "message"
FINISH_REASON_FIELD = "finish_reason"
REASONING_FIELD = "reasoning_content"

# A reasoning model puts its thinking in one of two places, and which one is a
# property of the serving stack rather than of the model: beside the answer in
# `reasoning_content`, or inline at the front of the answer, fenced in tags.
# The first costs nothing to ignore — this only ever reads `content`. The second
# has to be cut out, or the summary opens with the model talking to itself and
# the synthesizer reads the tags out loud.
#
# Handled whether or not `thinking` is off, because that setting is a request
# and not a guarantee: an endpoint that does not honour it still reasons, and
# what comes back is still not something to file as a summary.
REASONING_TAGS = "think|thinking|reasoning|thought"

# A whole fenced block, however the opening tag was attributed.
REASONING_BLOCK = re.compile(
    rf"<(?P<tag>{REASONING_TAGS})\b[^>]*>.*?</(?P=tag)\s*>",
    re.DOTALL | re.IGNORECASE,
)

# An opening tag with no closing one, which is a model cut off mid-thought.
# Everything from there on is thinking, so everything from there on goes.
UNCLOSED_REASONING = re.compile(
    rf"<(?:{REASONING_TAGS})\b[^>]*>.*\Z",
    re.DOTALL | re.IGNORECASE,
)

# How a model that reasons before answering is asked not to. `max_tokens` bounds
# what is generated, and on such a model the reasoning is generated too — so a
# budget that runs out mid-thought returns an empty `content` and a whole
# conversation's worth of nothing.
#
# Sent only to turn reasoning off, never to turn it on. An endpoint that has
# never heard of it is then one this never mentions it to, which is what keeps
# the request ordinary for everything that is not a reasoning model.
TEMPLATE_KWARGS_FIELD = "chat_template_kwargs"
ENABLE_THINKING_FIELD = "enable_thinking"

# What a response says when it stopped because it ran out of budget rather than
# because it had finished.
TRUNCATED = "length"

AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "

OK = 200
REDIRECTION = 300

# How much of a failing response to quote back. Enough to carry the endpoint's
# own explanation, short enough that an HTML error page does not become the log.
BODY_EXCERPT = 500
ELLIPSIS = "…"


class CompletionError(RuntimeError):
    """The endpoint did not return a usable completion."""


_session: aiohttp.ClientSession | None = None


async def complete(instruction: str, text: str) -> str:
    """
    Ask the model to do one thing to one piece of text, and return what it said.

    The instruction is the system message and the text is the user message,
    which is the split every chat model is trained on: one says how to read what
    follows, the other is what follows. Nothing here knows what either of them
    is about — the prompts live with the tool that chose them.
    """
    if not llm_cfg.configured:
        raise CompletionError(
            "no endpoint is configured; set LLM_API_BASE and LLM_MODEL"
        )

    session = _shared_session()
    payload = {
        MODEL_FIELD: llm_cfg.model,
        MESSAGES_FIELD: [
            {ROLE_FIELD: SYSTEM_ROLE, CONTENT_FIELD: instruction},
            {ROLE_FIELD: USER_ROLE, CONTENT_FIELD: text},
        ],
        MAX_TOKENS_FIELD: llm_cfg.max_output_tokens,
        TEMPERATURE_FIELD: llm_cfg.temperature,
    }

    if not llm_cfg.thinking:
        payload[TEMPLATE_KWARGS_FIELD] = {ENABLE_THINKING_FIELD: False}

    try:
        async with asyncio.timeout(llm_cfg.timeout_seconds):
            async with session.post(_endpoint(), json=payload, headers=_headers()) as response:
                body = await response.text()

                if not OK <= response.status < REDIRECTION:
                    raise CompletionError(
                        f"{llm_cfg.model} refused with {response.status}: "
                        f"{_excerpt(body)}"
                    )

                return _answer(body)
    except TimeoutError as exc:
        raise CompletionError(
            f"{llm_cfg.model} did not answer within {llm_cfg.timeout_seconds:.0f}s"
        ) from exc
    except aiohttp.ClientError as exc:
        # The message carries the URL but never the header the key is in.
        raise CompletionError(f"could not reach the endpoint: {exc}") from exc


def _answer(body: str) -> str:
    """
    The text out of a response, or a complaint about why there is none.

    An endpoint that answers 200 with something else is the failure worth being
    explicit about: an empty `choices` is what a refused or filtered request
    looks like on several of them, and returning "" for it would file an empty
    summary as though the conversation had been empty.
    """
    try:
        parsed: Any = json.loads(body)
        choice = parsed[CHOICES_FIELD][0]
        message = choice[MESSAGE_FIELD]
        content = message[CONTENT_FIELD]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise CompletionError(
            f"could not read a completion out of the response: {_excerpt(body)}"
        ) from exc

    said = str(content or "")
    answer = _without_reasoning(said)
    if answer:
        return answer

    raise CompletionError(_nothing_said(choice, message, said))


def _without_reasoning(said: str) -> str:
    """
    What the model actually answered, with any thinking cut off the front.

    Complete blocks first, then an unclosed one, because a model that ran out of
    budget mid-thought leaves an opening tag and no partner for it — and the
    text after that tag is thinking however it ends.
    """
    return UNCLOSED_REASONING.sub("", REASONING_BLOCK.sub("", said)).strip()


def _nothing_said(choice: Any, message: Any, said: str) -> str:
    """
    Why a 200 carried no answer, in the words of whoever has to fix it.

    Worth telling apart, because one of these is a setting and the others are
    not. A model that reasons before it answers spends `max_tokens` on the
    reasoning; run out mid-thought and the reasoning is all there is, which
    reads as a broken endpoint and is a number in the config file.
    """
    beside = message.get(REASONING_FIELD) if isinstance(message, dict) else None

    # Everything it said was thinking: whatever survived the tags was nothing.
    inline = bool(said.strip())

    truncated = (
        isinstance(choice, dict) and choice.get(FINISH_REASON_FIELD) == TRUNCATED
    )

    if beside or inline:
        return (
            f"the model spent its whole {llm_cfg.max_output_tokens}-token budget "
            f"reasoning and never began the answer. Raise "
            f"'{SETTINGS_KEY}.{LLM_SECTION}.{MAX_OUTPUT_TOKENS_KEY}', or set "
            f"'{SETTINGS_KEY}.{LLM_SECTION}.{THINKING_KEY}: false' to stop it "
            f"reasoning at all"
        )

    if truncated:
        return (
            f"the answer was cut off at {llm_cfg.max_output_tokens} tokens with "
            f"nothing usable in it; raise "
            f"'{SETTINGS_KEY}.{LLM_SECTION}.{MAX_OUTPUT_TOKENS_KEY}'"
        )

    return "the endpoint returned an empty completion"


def _endpoint() -> str:
    """The completions URL, however the root was written down."""
    return llm_cfg.base_url.rstrip(PATH_SEPARATOR) + COMPLETIONS_PATH


def _headers() -> dict[str, str]:
    """
    What the request carries besides the body.

    An empty key sends no header at all rather than `Bearer `, because an
    endpoint that wants no credential should not be handed an empty one to
    decide what to do with.
    """
    if not llm_cfg.api_key:
        return {}

    return {AUTHORIZATION_HEADER: BEARER_PREFIX + llm_cfg.api_key}


def _excerpt(body: str) -> str:
    """As much of a failing response as is worth putting in a log line."""
    trimmed = body.strip()
    if len(trimmed) <= BODY_EXCERPT:
        return trimmed

    return trimmed[:BODY_EXCERPT] + ELLIPSIS


def _shared_session() -> aiohttp.ClientSession:
    """
    The one session every completion goes through.

    Made on first use rather than at import, because a `ClientSession` binds to
    the running event loop and this module is imported while there is none.
    """
    global _session

    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()

    return _session


async def close() -> None:
    """
    Let go of the connection pool.

    Called on the way down by whichever tool has been using it. Idempotent, and
    a no-op in a process that never asked for a completion, so a deployment with
    nothing that summarizes never opens a session to close.
    """
    global _session

    if _session is not None and not _session.closed:
        await _session.close()

    _session = None
