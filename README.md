<p align="center">
  <img src="assets/miss-quote.png" alt="miss-quote" width="256">
</p>

# miss-quote

<p align="center">
  <strong>Writes down what happened in the voice channel, and then tells you about it.</strong>
</p>

<p align="center">
  📖 Full documentation: <a href="https://miss-quote.wars.beer">miss-quote.wars.beer</a>
</p>

![Python](https://img.shields.io/badge/Python-3.12-blue?style=for-the-badge&logo=python&logoColor=white)
![Discord.py](https://img.shields.io/badge/Discord.py-2.4%2B-5865F2?style=for-the-badge&logo=discord&logoColor=white)
![Wyoming](https://img.shields.io/badge/Wyoming-ASR-success?style=for-the-badge)
![Silero VAD](https://img.shields.io/badge/Silero%20VAD-ONNX-orange?style=for-the-badge)

**miss-quote** is a Discord bot that sits in on your D&D session and listens to the adventures, so the evening ends up with a record instead of in everyone's half-memory of it. When the bot leaves it summarizes what happened, and next time you can ask: "what happened last session" and a bard recounts the night. It gets up to other shenanigans too.

Walk into a film line and it says the line out loud, then asks the room where it came from and pays whoever gets it first. Swear and it fines you, out loud, *Demolition Man* style. It keeps a running tally of who owes what and publishes the standings under the voice channel's name.

Transcription is delegated to a [Wyoming](https://github.com/rhasspy/wyoming) ASR server rather than run in-process, so **this container** is a CPU-only workload with no model weights and no cache volume for them. That moves the GPU rather than removing it: the bot does nothing at all without a reachable ASR server, and in practice that server wants one. What it buys is a bot that schedules anywhere and a GPU that several things can share.

It is a hard fork of [Leehyunbin0131/Discord-Realtime-STT-Bot](https://github.com/Leehyunbin0131/Discord-Realtime-STT-Bot), which ran `faster-whisper` on a local GPU.

---

## Documentation

Everything lives on the site. This file is the short version.

| Page | What is on it |
|---|---|
| **[About](https://miss-quote.wars.beer/about/)** | How the pipeline is split, the transcript format, the speech path, the tool contract, and what changed from upstream |
| **[Installation](https://miss-quote.wars.beer/installation/)** | Prerequisites, the Discord application, Docker and Kubernetes, volumes, development, and cutting a release |
| **[Configuration](https://miss-quote.wars.beer/configuration/)** | `config.yaml` in full — every server key, every tool, every setting, and every environment variable |

## What it does

Audio handling is local and serial; transcription is remote and parallel. Everything downstream is a **tool** that reads the utterance stream or the sealed session, opted into per server:

| Tool | What it does |
|---|---|
| [`quotes`](https://miss-quote.wars.beer/configuration/#quotes) | Answers the channel with the film line it just walked into, then asks where it came from |
| [`summary`](https://miss-quote.wars.beer/configuration/#summary) | Writes down what happened once the bot leaves, and reads it back out loud when somebody asks |
| [`scoreboard`](https://miss-quote.wars.beer/configuration/#scoreboard) | Keeps a running balance per person and publishes the standings under the voice channel's name |
| [`tts`](https://miss-quote.wars.beer/configuration/#tts-tool) | Says things out loud. The only thing that plays anything |
| [`verbal-morality`](https://miss-quote.wars.beer/configuration/#verbal-morality) | Fines a speaker out loud for the wrong word, after *Demolition Man* |

## Quick start

```bash
docker run -d --name miss-quote \
  -e DISCORD_TOKEN="$DISCORD_TOKEN" \
  -e WYOMING_HOST=asr.internal \
  -e TTS_HOST=tts.internal \
  -v "$PWD/config.yaml:/config/config.yaml:ro" \
  -v miss-quote-transcripts:/transcripts \
  -v miss-quote-speech:/speech \
  ghcr.io/beer-wars/miss-quote:latest
```

Everything a deployment **points at** is an environment variable — see [`.env.example`](.env.example). Everything about how it **behaves**, and which servers it behaves that way in, is [`config.yaml`](config.yaml), which is commented in full.

A server that is not listed in `config.yaml` is never joined. That direction is deliberate: joining no server is something you notice and fix, while recording a server the bot should not have been in is not something you can take back.

Runtime requirements are a reachable Wyoming ASR server, a writable volume at `SPEECH_DIR`, and a single replica. The rest is on the [installation page](https://miss-quote.wars.beer/installation/).

## Development

```bash
make test                          # the suite, inside the image CI uses
make test PYTEST_ARGS="-k config"  # a narrower run
make validate-quotes               # the quote file and the config's additions; PyYAML only
make help                          # everything else
```

The suite runs in the container rather than against whatever interpreter is on the machine — there is no second recipe that can drift from the one CI uses, and no host Python to be the wrong version.

## Deployment

GitHub Actions builds the image and pushes it to GHCR. **Cutting a git tag is the deploy action**; pushing to `main` produces `latest` and a sha tag, neither of which is orderable.

```bash
git tag v0.1.0 && git push origin v0.1.0
```

## The site

The documentation site is a Jekyll build of [`docs/`](docs/), served by GitHub Pages at [miss-quote.wars.beer](https://miss-quote.wars.beer). To preview it locally:

```bash
cd docs && bundle install && bundle exec jekyll serve
```
