# 🎙️ miss-quote

![Python](https://img.shields.io/badge/Python-3.12-blue?style=for-the-badge&logo=python&logoColor=white)
![Discord.py](https://img.shields.io/badge/Discord.py-2.4%2B-5865F2?style=for-the-badge&logo=discord&logoColor=white)
![Wyoming](https://img.shields.io/badge/Wyoming-ASR-success?style=for-the-badge)
![Silero VAD](https://img.shields.io/badge/Silero%20VAD-ONNX-orange?style=for-the-badge)

> Transcribes Discord voice channels to a daily, per-speaker JSONL transcript.

Transcription is delegated to a [Wyoming](https://github.com/rhasspy/wyoming) ASR server rather than run in-process, so the bot is a CPU-only workload with no GPU, no model weights, and no cache volume. It is a hard fork of [Leehyunbin0131/Discord-Realtime-STT-Bot](https://github.com/Leehyunbin0131/Discord-Realtime-STT-Bot), which ran `faster-whisper` on a local GPU.

---

## How it works

```
                            Discord gateway
                                   │
                                   │  48 kHz stereo PCM, 20 ms frames
                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│  voice-recv router thread                    SERIAL             │
│                                                                 │
│  STTAudioSink.write ── soxr resample ──► 16 kHz mono   0.046 ms │
│                                                                 │
│  Holds the router lock across every speaker: nothing slow here. │
└────────────────────────────────┬────────────────────────────────┘
                                 │  loop.call_soon_threadsafe
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  event loop                                  SERIAL             │
│                                                                 │
│  Silero VAD, per 32 ms frame                           0.082 ms │
│         └─► per-speaker speech_buffer + ring-buffer pre-roll    │
│                                                                 │
│  ~4.9 ms of CPU per speaker per second of audio.                │
└────────────────────────────────┬────────────────────────────────┘
                                 │  speech → silence edge
                                 ▼
                        asyncio.create_task
                                 │
         ┌───────────────────────┼───────────────────────┐
         │                       │                       │
         ▼                       ▼                       ▼           PARALLEL
   ┌───────────┐           ┌───────────┐           ┌───────────┐
   │  Wyoming  │           │  Wyoming  │           │  Wyoming  │   one connection
   │utterance 1│           │utterance 2│           │utterance N│   per utterance
   └─────┬─────┘           └─────┬─────┘           └─────┬─────┘
         │                       │                       │        N ≤ MAX_CONCURRENT_
         └───────────────────────┼───────────────────────┘            TRANSCRIPTIONS
                                 │
                                 ▼
                   Wyoming ASR server, off-box
                   ($WYOMING_HOST:$WYOMING_PORT)
                                 │
                                 │  Transcript, ~70 ms per utterance
                                 ▼
                          TranscriptWriter
                                 │
                                 ▼
                 $TRANSCRIPT_DIR/YYYY-MM-DD.jsonl
```

Everything runs on one event loop in one process. The split that matters is between the two halves of the pipeline: **audio handling is local and serial, transcription is remote and parallel.**

### Local: serial, continuous, cheap

Resampling and VAD are ordinary blocking calls, run one frame at a time. They are a steady cost for as long as audio arrives, not a per-utterance burst — VAD has to see every frame, because VAD is what decides which frames are speech.

| Work | Cost | Rate, per speaker |
|---|---:|---|
| soxr resample, per 20 ms Discord frame | 0.046 ms | 50/s |
| Silero VAD, per 32 ms frame | 0.082 ms | 31.25/s |

That is **~4.9 ms of CPU per speaker per second of audio**, or about 0.5% of one core — 5% at ten concurrent speakers. Being serial costs nothing at this magnitude, which is why there is no worker process: a process boundary would cost more in serialization than the work it isolated.

Resampling runs on voice-recv's router thread, which holds a lock across all speakers, so nothing slow may be added there. Frames reach the event loop via `loop.call_soon_threadsafe`.

### Remote: parallel, bounded

At each speech-to-silence edge the buffered utterance is handed to `asyncio.create_task` and the coroutine immediately parks on socket I/O — the loop is free in the same tick. Nothing in this process ever blocks on transcription.

The ASR server accepts overlapping utterances, so speakers do not queue behind one another; measured against a GPU-backed Wyoming server, eight simultaneous 0.88 s utterances completed in 223 ms against 555 ms if run serially. A single utterance round-trips in about 70 ms.

`MAX_CONCURRENT_TRANSCRIPTIONS` caps how many are in flight. A further utterance ending while the cap is reached parks on the semaphore — it does not stall the loop and does not drop audio, it simply waits to open its connection. The bound exists so a busy channel cannot fan out unbounded connections against an ASR that other services may share; throughput gains past four are marginal anyway.

Upstream got this backwards: it called `transcribe()` inline in the per-frame loop, so the expensive remote half ran serially while audio backed up in a queue until frames were silently dropped.

---

## Transcript format

JSON Lines, one object per utterance, appended and flushed as produced:

```json
{"ts":"2026-07-26T21:14:03.412-07:00","user_id":1234567890,"user":"someone","channel":"general-voice","text":"that should work"}
```

Files roll over on the local calendar date, resolved through `TZ`, and timestamps carry an explicit UTC offset. A session spanning midnight writes into two files. `user_id` is recorded alongside the display name because display names change.

---

## Configuration

Every setting is read from the environment; `.env` is loaded if present. Nothing about a particular deployment is baked into the image, so the same image runs anywhere the variables below point it at.

### Discord

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | — | Bot token. **Required** — the bot exits immediately without it |
| `COMMAND_PREFIX` | `!` | Prefix for the `!join` / `!leave` commands |
| `AUTOJOIN` | `true` | Join when a human enters a voice channel; leave when it empties. Accepts `true/false`, `1/0`, `yes/no`, `on/off` |

### ASR

| Variable | Default | Purpose |
|---|---|---|
| `WYOMING_HOST` | `localhost` | Hostname or service name of the Wyoming ASR server |
| `WYOMING_PORT` | `10300` | Wyoming's conventional port |
| `STT_LANGUAGE` | `en` | Sent as `Transcribe.language` |
| `MAX_CONCURRENT_TRANSCRIPTIONS` | `4` | Ceiling on in-flight utterances, so a busy channel cannot open unbounded connections against a shared ASR |

### Transcripts

| Variable | Default | Purpose |
|---|---|---|
| `TRANSCRIPT_DIR` | `/transcripts` | Directory the daily files are written to |
| `TZ` | `America/Los_Angeles` | Rollover boundary and the offset stamped on each line |
| `RETENTION_DAYS` | `-1` | Days to keep. `-1`, or any value below `1`, keeps forever |

### Speech segmentation

| Variable | Default | Purpose |
|---|---|---|
| `SPEECH_FLUSH_TIMEOUT_SECONDS` | `2.0` | Transcribe a speech buffer that stopped receiving audio, e.g. a speaker who muted mid-sentence |
| `USER_TIMEOUT_SECONDS` | `60` | Discard per-user VAD state after this much silence |
| `LOG_LEVEL` | `INFO` | Standard Python log levels |

VAD thresholds, the pre-roll depth, and the Wyoming chunk size are deliberately **not** environment variables — they are tied to Silero's fixed 512-sample frame and live in `config.py`.

### Retention

Pruning is **off by default**. Any value below `1` disables it entirely, so `0` is a no-op rather than "delete everything" and a mis-set variable cannot destroy the archive. When set to a positive `N`, files older than `N` days are deleted, aged by the **filename date** rather than mtime — the filename is the authoritative record of the day covered, while mtime misjudges a file appended to late or restored from backup. Pruning runs at startup and on each date rollover.

### Auto-join

With `AUTOJOIN` enabled the bot connects as soon as a non-bot member enters a voice channel, and disconnects once the channel empties of humans. A bot can occupy only one voice channel per guild, so if a second channel becomes active it stays where it is rather than hopping, which would fragment both transcripts.

The `!join` and `!leave` commands remain available either way. They require **Message Content Intent** to be enabled in the Discord Developer Portal.

---

## Changes from upstream

Upstream ran `faster-whisper` in-process on a GPU. Moving transcription to a network call removed the reason for most of the machinery around it.

| Area | Upstream | Here |
|---|---|---|
| Transcription | `faster-whisper` in-process, GPU | Wyoming client, one connection per utterance |
| Concurrency | Child process + three IPC queues, to keep blocking inference off the event loop | One process, one event loop; each utterance is a bounded `asyncio` task |
| Dispatch | Transcription called inline in the per-frame loop, serializing every speaker behind one utterance until the audio queue overflowed and dropped frames | Per-utterance tasks bounded by a semaphore, so speakers overlap |
| Resampling | `torchaudio` | `soxr` |
| VAD | Silero via `torch.hub` | Silero via `onnxruntime`, model vendored in-repo |
| Output | Logged and printed; never persisted | Per-day JSONL file, flushed per utterance |
| Deployment | systemd unit | Container image |

Removed outright: the multiprocessing layer and its queues, the STT health-check thread and its supervisor, `torch` / `torchaudio` / `faster-whisper`, the model and fallback-model settings (`STT_MODEL_ID`, `STT_DEVICE`, `STT_COMPUTE_TYPE`, `STT_BEAM_SIZE`, every `STT_FALLBACK_*`), all `*_QUEUE_MAXSIZE` tuning, `RESULT_POLL_INTERVAL`, `STT_HEALTH_CHECK_INTERVAL`, `SHUTDOWN_TIMEOUT_SECONDS`, and the systemd deployment.

Kept intact because they are the non-obvious part: `stt/user_state.py`'s per-user VAD state machine with stale-speech flushing, and `audio/ring_buffer.py`'s pre-roll buffer, which is what stops the first syllable being clipped.

Added: `AUTOJOIN`, `RETENTION_DAYS`, `TRANSCRIPT_DIR`, `TZ`, `WYOMING_HOST`, `WYOMING_PORT`, `MAX_CONCURRENT_TRANSCRIPTIONS`.

> **Note on the vendored VAD model.** Silero v5's ONNX graph scores the current frame *together with* the trailing 64 samples of the previous one. Fed a bare 512-sample frame it does not error — it silently returns near-zero probability on unmistakable speech, and the bot transcribes nothing. `stt/vad.py` carries that context between calls, and `tests/test_vad.py` guards it with real speech; silence-based tests pass either way and will not catch a regression.

---

## Project structure

```
miss-quote/
├── main.py                    # Entry point
├── config.py                  # Grouped configuration (dataclasses)
├── bot/
│   ├── client.py              # Bot setup, voice lifecycle, auto-join policy
│   └── audio_sink.py          # AudioSink + resampling bridge
├── audio/
│   ├── resampler.py           # soxr 48 kHz stereo to 16 kHz mono
│   └── ring_buffer.py         # Pre-speech context buffer
├── stt/
│   ├── vad.py                 # Silero VAD via onnxruntime
│   ├── user_state.py          # Per-user VAD state machine
│   ├── processor.py           # Segmentation and bounded dispatch
│   ├── wyoming_client.py      # Per-utterance Wyoming round-trip
│   └── models/
│       └── silero_vad.onnx    # Vendored (~2 MB)
├── transcript/
│   └── writer.py              # Daily-rollover JSONL appender + retention
└── utils/
    └── logging.py
```

The Silero model is vendored rather than installed, because the `silero-vad` package declares `torch` even in ONNX mode.

---

## Development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The ASR path is the riskiest integration and is worth exercising on its own, before any Discord wiring. Point `WYOMING_HOST` at any reachable Wyoming server and send the bundled speech fixture through the client:

```bash
WYOMING_HOST=<asr-host> python -c "
import asyncio, wave
from stt.wyoming_client import transcribe
with wave.open('tests/fixtures/speech_16k_mono.wav', 'rb') as f:
    pcm = f.readframes(f.getnframes())
print(asyncio.run(transcribe(pcm)))
"
```

A correct setup prints `That should work.` in well under a second.

---

## Deployment

GitHub Actions builds the image and pushes it to GHCR for the repository it runs in; no registry configuration is required beyond the workflow's `packages: write` permission. New GHCR packages are private by default, so the package must be made public once, after the first run, unless the cluster is given a pull secret.

Runtime requirements:

- **A reachable Wyoming ASR server** — set `WYOMING_HOST` and `WYOMING_PORT`. There is no default that will work out of the box.
- **A writable volume at `TRANSCRIPT_DIR`.** Use a shared (`ReadWriteMany`) volume if anything else will need to read the transcripts; a single-writer volume locks them to this pod and forces an export step later.
- **A single replica.** Two instances would double-join the voice channel and double-write the transcript.
- **No GPU and no node constraints** — transcription is a network call.

**Cutting a git tag is the deploy action.** Pushing to `main` produces `latest` and a sha tag, neither of which is orderable; a release needs a semver tag, which is what a pinned deployment references and what dependency automation can raise a bump against:

```bash
git tag v0.1.0 && git push origin v0.1.0
```
