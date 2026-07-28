# Todo

Known compromises to come back to. Each entry says what is wrong, why it was accepted, and what "done" looks like.

## Replace the patched voice-receive internals

**Current state.** `bot/voice_patches.py` monkeypatches two `PacketDecoder` methods at startup: `_process_packet` to decrypt DAVE frames before Opus sees them, and `pop_data` to drop packets the decoder rejects instead of ending voice receive.

**Why.** Discord has required DAVE, its MLS end-to-end encryption, on every non-stage voice call since 2026-03-02; a client that advertises no support is rejected at the handshake with close code 4017. discord.py negotiates the MLS session and holds it on the connection state, but `discord-ext-voice-recv`, pinned at `ac04ea7b`, has no DAVE support at all, so it fed still-encrypted payloads to the Opus decoder. The repository's last commit is 2025-06-18 and both fixes sit unmerged: [PR #54](https://github.com/imayhaveborkedit/discord-ext-voice-recv/pull/54) for decryption, [PR #57](https://github.com/imayhaveborkedit/discord-ext-voice-recv/pull/57) for the decode guard.

**What this costs.** The patches reach into library internals — `PacketDecoder._process_packet`, `_decode_packet`, `_get_cached_member`, `_last_seq`/`_last_ts` — none of which are public API. `_process_packet` in particular is a reimplementation of the pinned version with the speaker resolved before decoding rather than after, so it silently drifts if the pin ever moves. Both patches are idempotent and guarded, but neither is a substitute for upstream support.

**What done looks like.** Either #54 and #57 land and the pin moves past them, or a maintained replacement for `discord-ext-voice-recv` appears with DAVE support. At that point `bot/voice_patches.py` goes away and `tests/test_voice_patches.py` with it. The watchdog in `bot/client.py` should stay regardless, since it covers causes neither patch addresses.

## Verify audio decodes to the right words

**Current state.** Unverified. No transcript has ever been produced.

**Why it is open.** [Issue #53](https://github.com/imayhaveborkedit/discord-ext-voice-recv/issues/53) reports that on discord.py 2.7.1 voice packets can decode to audible gibberish with no error raised. Until DAVE decryption worked there was no way to reach that question, because every packet failed earlier and louder.

**What done looks like.** Someone speaks in a voice channel and the resulting JSONL line matches what they said. The failure mode to watch for is confident nonsense rather than an empty directory.
