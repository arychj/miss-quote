FROM python:3.12-slim AS builder

# git is needed for the VCS-pinned discord-ext-voice-recv requirement.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# The test stage sits between the two so that the image the registry gets stays
# the last stage in the file: a bare `docker build` still resolves to it, and
# nothing that only the tests need reaches it.
FROM python:3.12-slim AS test

# The same reason the runtime stage installs it, and the reason the tests cannot
# run on a bare runner: discord.py ships an Opus binary for macOS and Windows
# and falls back to the system library on Linux.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app

# Only the packages the runtime set does not already have. The runtime set
# itself came in with the layer above, and re-reading requirements.txt here
# would send pip back to git for the pinned revision in it.
COPY requirements-test.txt ./

RUN pip install --no-cache-dir -r requirements-test.txt

COPY pytest.ini ./

# The shipped config is a fixture as much as a default — one test parses it to
# hold the example in the repository to the same rules the parser enforces.
COPY config.yaml ./

COPY src/ src/
COPY scripts/ scripts/
COPY tests/ tests/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Split so that `docker run <image> -k some_test` appends to the arguments
# rather than replacing the command.
ENTRYPOINT ["pytest"]

CMD ["-q"]


FROM python:3.12-slim AS runtime

# libopus0 is required in both directions: discord-ext-voice-recv decodes Opus
# in-process, and tools that answer out loud encode it on the way back.
#
# ffmpeg is deliberately absent. It is the usual way to play audio through
# discord.py, but only because it is the usual way to decode a file first —
# synthesized speech is already raw PCM, so there is nothing to transcode.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app

COPY src/ src/

# The package is on the path rather than installed: an install step would want
# the dependencies resolved a second time, and they are already in the layer
# above. `python -m miss_quote` runs the same entry point either way.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    TRANSCRIPT_DIR=/transcripts \
    SUMMARY_DIR=/summaries \
    SPEECH_DIR=/speech \
    CREDITS_FILE=/credits/credits.json \
    QUOTES_FILE=/app/src/miss_quote/resources/quotes.csv

# The speech directories and the credits directory are created either way.
# Mounting a volume over each is what makes rendered speech and the tally
# outlive the pod; without one the bot re-synthesizes each phrase once per
# restart, which costs a delay rather than a failure, and forgives every fine
# anybody has earned.
#
# Both speech subdirectories are made rather than just the root, so that a
# deployment mounting one volume at /speech still has somewhere to put a chime
# by hand. Summaries get their own root rather than a directory inside the
# transcripts, so the account of an evening and the record of everything said in
# it can be mounted and shared on different terms.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /transcripts /summaries /speech/cache /speech/chimes /credits \
    && chown -R bot:bot /transcripts /summaries /speech /credits

USER bot

CMD ["python", "-m", "miss_quote"]
