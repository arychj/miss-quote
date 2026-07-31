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
from typing import Any

import aiohttp

from miss_quote.config import llm_cfg
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
        MAX_TOKENS_FIELD: llm_cfg.max_tokens,
        TEMPERATURE_FIELD: llm_cfg.temperature,
    }

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
        choices = parsed[CHOICES_FIELD]
        content = choices[0][MESSAGE_FIELD][CONTENT_FIELD]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise CompletionError(
            f"could not read a completion out of the response: {_excerpt(body)}"
        ) from exc

    answer = str(content).strip()
    if not answer:
        raise CompletionError("the endpoint returned an empty completion")

    return answer


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
