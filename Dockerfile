FROM python:3.12-slim AS builder

# git is needed for the VCS-pinned discord-ext-voice-recv requirement.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

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

COPY audio/ audio/
COPY bot/ bot/
COPY stt/ stt/
COPY tools/ tools/
COPY transcript/ transcript/
COPY tts/ tts/
COPY utils/ utils/
COPY config.py main.py ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRANSCRIPT_DIR=/transcripts \
    TTS_CACHE_DIR=/cache/tts

# The cache is created either way. Mounting a volume over it is what makes
# rendered speech outlive the pod; without one the bot re-synthesizes each
# phrase once per restart, which costs a delay rather than a failure.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /transcripts /cache/tts \
    && chown -R bot:bot /transcripts /cache

USER bot

CMD ["python", "main.py"]
