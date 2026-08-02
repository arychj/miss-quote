---
layout: page
title: Installation
eyebrow: Getting it running
lede: What it has to be pointed at, how to run it, and what has to survive a restart. Nothing about a particular deployment is baked into the image, so the same image runs anywhere the variables point it at.
description: Prerequisites, Discord application setup, running miss-quote under Docker or Kubernetes, the volumes that have to persist, development, and cutting a release.
---

## What you need first

miss-quote is a client. It does not transcribe, synthesize, or summarize anything itself — it points at services that do, and there is **no default that will work out of the box** for any of them.

| Requirement | Why | Needed when |
|---|---|---|
| **A Discord bot application** and its token | The thing that joins the channel | Always |
| **A reachable Wyoming ASR server** | Transcription, one connection per utterance | Always |
| A container runtime, or Python 3.12 | It ships as an image | Always |
| **A reachable Wyoming TTS server** | Anything said out loud | Any server enabling the `tts` tool |
| **An OpenAI-compatible chat endpoint** | Summaries and retellings | Any server enabling `summary` |

**This pod** needs no GPU and no node constraints — transcription and synthesis are network calls, and the only model it runs itself is Silero VAD, under `onnxruntime` on the CPU, vendored in the image.

That is a statement about *where* the GPU lives, not about whether you need one. The three services above are the ones that do the expensive work, and a Wyoming ASR fast enough to keep up with a conversation is a GPU workload wherever you put it. Budget for it; just budget for it once, on a host several things can share, rather than pinning this bot to it.

**A single replica.** Two instances would double-join the voice channel and double-write the transcript.

## The Discord application

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications) and add a bot to it.
2. Copy the bot token — this becomes `DISCORD_TOKEN`.
3. Enable **Message Content Intent** under *Bot → Privileged Gateway Intents*. The `!join` / `!leave` and `!start-transcribing` / `!stop-transcribing` commands do not work without it.
4. Invite the bot with the scopes `bot` and `applications.commands`.

### Permissions

| Permission | Where | What it costs to omit |
|---|---|---|
| **Connect**, **Speak** | Each voice channel | Everything — the bot cannot join or answer |
| **View Channel**, **Send Messages** | The text channel a summary is posted to | Summaries are written to disk and never posted |
| **Set Voice Channel Status** | Each voice channel | The scoreboard keeps counting and logs once per change; the standings never reach the channel |

**Set Voice Channel Status** is the one worth reading twice. It is not Manage Channels, and a voice channel has no topic — what the scoreboard sets is the channel *status*, the line the client shows beneath a voice channel's name.

## Running it

The image is published to GHCR for the repository it is built in.

### Docker

```bash
docker run -d --name miss-quote \
  -e DISCORD_TOKEN="$DISCORD_TOKEN" \
  -e WYOMING_HOST=asr.internal \
  -e TTS_HOST=tts.internal \
  -e LLM_API_BASE=http://llm.internal:8080/v1 \
  -e LLM_MODEL=your-model \
  -e TZ=America/Los_Angeles \
  -v "$PWD/config.yaml:/config/config.yaml:ro" \
  -v miss-quote-transcripts:/transcripts \
  -v miss-quote-summaries:/summaries \
  -v miss-quote-credits:/credits \
  -v miss-quote-speech:/speech \
  ghcr.io/beer-wars/miss-quote:latest
```

New GHCR packages are private by default, so the package must be made public once, after the first run, unless the runtime is given a pull secret.

### Docker Compose

```yaml
services:
  miss-quote:
    image: ghcr.io/beer-wars/miss-quote:latest
    restart: unless-stopped
    environment:
      DISCORD_TOKEN: ${DISCORD_TOKEN:?set it in .env}
      WYOMING_HOST: asr.internal
      TTS_HOST: tts.internal
      LLM_API_BASE: http://llm.internal:8080/v1
      LLM_MODEL: your-model
      TZ: America/Los_Angeles
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - transcripts:/transcripts
      - summaries:/summaries
      - credits:/credits
      - speech:/speech

volumes:
  transcripts:
  summaries:
  credits:
  speech:
```

`.env` is loaded if present, so the token can live there rather than in the compose file.

### Kubernetes

`config.yaml` is meant to be a ConfigMap, mounted at `/config/config.yaml`; the token is a Secret. Keep `settings.llm.timeout_seconds` well under `terminationGracePeriodSeconds` — a session sealed as the pod goes down is summarized inside the shutdown, so a whole LLM round trip runs inside the grace period and can be killed by it.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: miss-quote
spec:
  replicas: 1                          # Two would double-join and double-write.
  strategy:
    type: Recreate
  selector:
    matchLabels: { app: miss-quote }
  template:
    metadata:
      labels: { app: miss-quote }
    spec:
      terminationGracePeriodSeconds: 180
      containers:
        - name: miss-quote
          image: ghcr.io/beer-wars/miss-quote:v0.1.0
          env:
            - name: DISCORD_TOKEN
              valueFrom:
                secretKeyRef: { name: miss-quote, key: discord-token }
            - name: WYOMING_HOST
              value: wyoming-whisper.speech.svc.cluster.local
            - name: TTS_HOST
              value: wyoming-piper.speech.svc.cluster.local
            - name: LLM_API_BASE
              value: http://vllm.llm.svc.cluster.local:8000/v1
            - name: LLM_MODEL
              value: your-model
            - name: TZ
              value: America/Los_Angeles
          volumeMounts:
            - { name: config,      mountPath: /config }
            - { name: transcripts, mountPath: /transcripts }
            - { name: summaries,   mountPath: /summaries }
            - { name: credits,     mountPath: /credits }
            - { name: speech,      mountPath: /speech }
      volumes:
        - name: config
          configMap: { name: miss-quote-config }
        - name: transcripts
          persistentVolumeClaim: { claimName: miss-quote-transcripts }
        - name: summaries
          persistentVolumeClaim: { claimName: miss-quote-summaries }
        - name: credits
          persistentVolumeClaim: { claimName: miss-quote-credits }
        - name: speech
          persistentVolumeClaim: { claimName: miss-quote-speech }
```

The file is read once at startup, so editing the ConfigMap means restarting the pod.

## Volumes

Four directories, and what each costs to leave out:

| Mount | Default path | Needed when | Cost of omitting |
|---|---|---|---|
| Transcripts | `/transcripts` | Always | The archive is lost at every restart |
| **Speech cache** | `/speech` | Anything speaks | **Every phrase is synthesized again every time it is said**, and `prewarm` does nothing at all — reported as an error at startup, not a warning |
| Summaries | `/summaries` | `summary` is enabled | The archive is lost; each summary is still posted to its channel when written |
| Credits | `/credits` | `scoreboard` is enabled | The tally is forgiven at every restart |

**`SPEECH_DIR` is load-bearing, not an optimisation.** Without a writable volume there, every announcement is a round trip to the TTS server instead of a file read.

Use a shared (`ReadWriteMany`) volume for the transcripts if anything else will need to read them; a single-writer volume locks them to this pod and forces an export step later.

`SPEECH_DIR` holds two subdirectories with different owners: `cache/` is rendered speech, written and reaped by the bot, and `chimes/` is where **you** put 16-bit WAVs by hand. Nothing writes to or reaps `chimes/`, so a clip put there deliberately is never on a retention clock meant for a phrase said once.

## Environment

Everything a deployment *points at* is an environment variable; everything about how it *behaves* is `config.yaml`. The minimum is a token, an ASR host, and a config file with at least one server in it.

```bash
DISCORD_TOKEN=...          # Required — the bot exits immediately without it
CONFIG_FILE=/config/config.yaml
WYOMING_HOST=asr.internal
TZ=America/Los_Angeles
```

The full table — every variable, its default, and what reads it — is on the [Configuration page]({{ '/configuration/#environment' | relative_url }}).

## Verifying it

The ASR path is the riskiest integration and is worth exercising on its own, before any Discord wiring. Point `WYOMING_HOST` at any reachable Wyoming server and send the bundled speech fixture through the client:

```bash
PYTHONPATH=src WYOMING_HOST=<asr-host> python -c "
import asyncio, wave
from miss_quote.stt.wyoming_client import transcribe
with wave.open('tests/fixtures/speech_16k_mono.wav', 'rb') as f:
    pcm = f.readframes(f.getnframes())
print(asyncio.run(transcribe(pcm)))
"
```

A correct setup prints `That should work.` in well under a second.

On startup the bot reconciles the config file against the servers it is actually in and says so. Four things can be wrong and none of them raise: an entry would not parse, nothing is configured, a server is configured but the bot was never invited, or the bot is in a server nobody configured. **Read the startup log** — every misconfiguration this project can detect is reported there rather than left to be discovered by noticing an empty directory.

## Development

```bash
make test
```

The suite runs in the container, not on the machine. `make test` builds the `test` stage of the same Dockerfile the published image comes from and runs pytest inside it, which is exactly what CI does — there is no second recipe that can drift from this one, and no host Python to be the wrong version. The stage carries what a test run needs and the published image does not: pytest, the tests themselves, `scripts/`, and the sample `config.yaml` that one of them parses.

It also settles the awkward dependency. Rendered speech is encoded with libopus rather than handed to discord.py as samples, so `tests/test_opus.py` needs the library loadable; discord.py ships a binary for macOS and Windows and falls back to the system one on Linux, which is how a suite that passes on a laptop fails on a bare runner. The image has it either way.

A narrower run goes through the same target:

```bash
make test PYTEST_ARGS="-k config -vv"
make shell                              # a prompt inside the test image
make build                              # the image that gets published
make help                               # everything else
```

Working on the code with an editor that wants the imports resolved still wants a local environment, and `requirements-dev.txt` is that environment — it is not what the tests run against.

### Changing the quote list

Changing the quote list needs none of it. `make validate-quotes` is standard library only, runs against the host Python rather than the image, and is what CI runs on a quote-file change — the point being an answer in seconds instead of after an image build.

```bash
make validate-quotes                                   # the shipped file
python scripts/validate_quotes.py /path/to/yours.yaml  # one you mount over it
```

The `Validate Quotes` workflow runs it on every push and pull request that touches the file, and takes a path as a `workflow_dispatch` input for checking a list that lives outside this repository. What it checks, and why each rule exists, is under [quotes]({{ '/configuration/#the-quote-file' | relative_url }}).

## Deployment

GitHub Actions builds the image and pushes it to GHCR for the repository it runs in; no registry configuration is required beyond the workflow's `packages: write` permission.

**Cutting a git tag is the deploy action.** Pushing to `main` produces `latest` and a sha tag, neither of which is orderable; a release needs a semver tag, which is what a pinned deployment references and what dependency automation can raise a bump against:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

## Upgrading

> **From `TTS_CACHE_DIR`.** One directory used to hold both rendered speech and the clips an operator put there by hand; it is now `SPEECH_DIR` with a subdirectory for each. Point the volume at `/speech` and the cache rebuilds itself on the usual terms — nothing is lost but the first synthesis of each phrase, and `prewarm` pays most of that before anyone is waiting. **A chime has to be moved by hand** into `SPEECH_DIR/chimes`, because nothing reads the old path any more; one left behind is reported at startup as missing rather than guessed at. The old directory is not read, not migrated, and not deleted — it is yours to remove once you are satisfied.

> **To the `tts` tool.** Playing audio used to be something every tool could do; it is now one tool, and **a server that does not enable `tts` says nothing.** Add `tts: {enabled: true}` beside whatever already speaks. Fines are still counted and rounds are still paid without it, and the log says so once at startup, so a server that goes quiet after an upgrade has a line explaining why. **A `chime:` setting also loses its extension** — `chime.wav` becomes `chime` — and one left as it was is called out by name rather than reported as a file that is not there. Clips written by a version older still are `.wav` and cannot be read at all; anything left in the new cache directory ages out on the retention clock whatever it is named.

<nav class="page-nav" aria-label="Previous and next page">
  <a class="page-nav-prev" rel="prev" href="{{ '/about/' | relative_url }}"><span class="page-nav-label">← Previous</span><strong>About</strong><span class="page-nav-blurb">How the pipeline is put together</span></a>
  <a class="page-nav-next" rel="next" href="{{ '/configuration/' | relative_url }}"><span class="page-nav-label">Next →</span><strong>Configuration</strong><span class="page-nav-blurb">The file it reads, every tool, and every setting</span></a>
</nav>
