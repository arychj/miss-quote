FROM python:3.12-slim AS builder

# git is needed for the VCS-pinned discord-ext-voice-recv requirement.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

# libopus0 is required: discord-ext-voice-recv decodes Opus in-process.
# ffmpeg is deliberately absent — it is a playback dependency and this bot is
# receive-only.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app

COPY audio/ audio/
COPY bot/ bot/
COPY stt/ stt/
COPY transcript/ transcript/
COPY utils/ utils/
COPY config.py main.py ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRANSCRIPT_DIR=/transcripts

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /transcripts \
    && chown bot:bot /transcripts

USER bot

CMD ["python", "main.py"]
