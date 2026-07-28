# Todo

Known compromises to come back to. Each entry says what is wrong, why it was accepted, and what "done" looks like.

## Restore end-to-end encryption for voice (DAVE)

**Current state.** `bot/voice_patches.disable_dave()` clears `discord.voice_state.has_dave` at startup, so the bot advertises DAVE protocol version 0 in the voice IDENTIFY and Discord falls back to transport-only encryption.

**Why.** discord.py 2.7.1 ships `davey` and advertises DAVE version 1. `discord-ext-voice-recv`, pinned at `ac04ea7b`, has no DAVE support whatsoever: grepping the installed package for `dave` or `mls` returns nothing. It therefore hands still-MLS-encrypted payloads to the Opus decoder, every packet fails as `corrupted stream`, and the bot transcribes nothing. Advertising version 0 is what makes voice receive work at all today.

**What this costs.** Media is still encrypted on the wire with `aead_xchacha20_poly1305_rtpsize`, so a passive network observer sees nothing. What is given up is confidentiality *from Discord*: the voice server holds the media key and can decrypt the audio, which is how Discord worked before DAVE rolled out in 2024.

DAVE is negotiated per call, so this downgrades encryption for **every participant in any channel the bot joins**, not just the bot. Anyone in those channels should know that.

**What done looks like.** Any one of:

- Upstream merges DAVE support. [PR #54](https://github.com/imayhaveborkedit/discord-ext-voice-recv/pull/54) adds decryption in `opus.py` and is open; the repository's last commit is 2025-06-18, so this may never land.
- We implement MLS decryption ourselves against `davey` and drop `disable_dave()`.
- A maintained replacement for `discord-ext-voice-recv` appears with DAVE support.

Whichever path, the finish line is `disable_dave()` deleted and `tests/test_voice_patches.py::test_disable_dave_stops_advertising_e2ee` gone with it.

## Drop the packet-decode guard when upstream fixes the router

**Current state.** `bot/voice_patches.guard_packet_decoding()` wraps `PacketDecoder.pop_data` so an undecodable packet returns `None` instead of raising.

**Why.** `PacketRouter._do_run` has no per-packet guard and `PacketRouter.run` calls `stop_listening()` from its `finally`, so one bad packet permanently ends voice receive for the connection. Known triggers beyond DAVE include a participant starting their camera ([#49](https://github.com/imayhaveborkedit/discord-ext-voice-recv/issues/49)) and the plain `corrupted stream` reports in [#43](https://github.com/imayhaveborkedit/discord-ext-voice-recv/issues/43).

**What done looks like.** [PR #57](https://github.com/imayhaveborkedit/discord-ext-voice-recv/pull/57) lands upstream and the pin moves past it, at which point the patch can go. The watchdog in `bot/client.py` should stay regardless, since it covers causes we have not seen.

## Verify audio actually decodes correctly

**Current state.** Unverified. No transcript has ever been produced.

**Why it is open.** [Issue #53](https://github.com/imayhaveborkedit/discord-ext-voice-recv/issues/53) reports that on discord.py 2.7.1 voice packets decode to audible gibberish with no error raised, which we would not have hit yet because DAVE was failing louder and earlier. If it is real, the symptom is transcripts full of nonsense rather than an empty directory.

**What done looks like.** Someone speaks in a voice channel and the resulting JSONL line matches what they said.
