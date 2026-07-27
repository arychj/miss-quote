"""
Discord bot client setup, voice lifecycle, and command handling.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

import discord
import discord.ext.voice_recv
from discord.ext import commands

from bot.audio_sink import STTAudioSink
from config import discord_cfg
from stt.processor import STTProcessor
from transcript.writer import TranscriptWriter
from utils.logging import get_logger

logger = get_logger(__name__)


class VoiceAction(Enum):
    """What a voice-state change should make the bot do."""
    NONE = "none"
    JOIN = "join"
    LEAVE = "leave"


def humans_in(channel: Any) -> int:
    """Count non-bot members currently in a voice channel."""
    if channel is None:
        return 0
    return sum(1 for member in channel.members if not member.bot)


def plan_voice_action(
    before: Any,
    after: Any,
    current_channel: Any,
    autojoin: bool,
) -> tuple[VoiceAction, Any]:
    """
    Decide how to react to another member's voice-state change.

    Pure so the policy can be tested without a gateway connection. A bot may
    occupy only one voice channel per guild, so once connected it stays put
    rather than hopping to a newly active channel — hopping would fragment both
    transcripts.
    """
    if not autojoin or before.channel == after.channel:
        return VoiceAction.NONE, None

    if current_channel is None:
        if after.channel is not None:
            return VoiceAction.JOIN, after.channel
        return VoiceAction.NONE, None

    if before.channel == current_channel and humans_in(current_channel) == 0:
        return VoiceAction.LEAVE, current_channel

    return VoiceAction.NONE, None


class _Bot(commands.Bot):
    """A Bot that drains the processor before the loop goes away."""

    def __init__(self, processor: STTProcessor, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._processor = processor

    async def close(self) -> None:
        # SIGTERM lands here on pod termination; buffered speech is only lost if
        # it is not flushed before the loop stops.
        await self._processor.stop()
        await super().close()


class STTBot:
    """Wraps a commands.Bot with voice-transcription lifecycle management."""

    def __init__(self) -> None:
        self._writer = TranscriptWriter()
        self._processor = STTProcessor(self._writer)

        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True

        self._bot = _Bot(
            self._processor,
            command_prefix=discord_cfg.command_prefix,
            intents=intents,
        )
        self._register_events()
        self._register_commands()

    # ── public ────────────────────────────────────

    def run(self) -> None:
        """Start the bot (blocking)."""
        if not discord_cfg.token:
            logger.critical("DISCORD_TOKEN is not set. Aborting.")
            return
        self._bot.run(discord_cfg.token, log_handler=None)

    # ── events ────────────────────────────────────

    def _register_events(self) -> None:
        bot = self._bot

        @bot.event
        async def on_ready() -> None:
            logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
            self._processor.start(asyncio.get_running_loop())
            if discord_cfg.autojoin:
                await self._join_active_channels()

        @bot.event
        async def on_voice_state_update(
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
        ) -> None:
            if bot.user is not None and member.id == bot.user.id:
                if before.channel and not after.channel:
                    self._processor.flush_all("bot disconnected")
                return

            if before.channel and before.channel != after.channel:
                self._processor.flush_user(member.id, "user left channel")

            guild_voice = member.guild.voice_client
            current = guild_voice.channel if guild_voice else None
            action, channel = plan_voice_action(
                before, after, current, discord_cfg.autojoin
            )

            if action is VoiceAction.JOIN:
                await self._connect(channel)
            elif action is VoiceAction.LEAVE:
                logger.info("Channel '%s' is empty; leaving.", channel)
                await self._disconnect(guild_voice)

    async def _join_active_channels(self) -> None:
        """Pick up channels that already had people in them when the bot started."""
        for guild in self._bot.guilds:
            if guild.voice_client is not None:
                continue
            for channel in guild.voice_channels:
                if humans_in(channel) > 0:
                    await self._connect(channel)
                    break

    # ── voice lifecycle ───────────────────────────

    async def _connect(self, channel: discord.abc.Connectable) -> None:
        try:
            voice_client = await channel.connect(
                cls=discord.ext.voice_recv.VoiceRecvClient,
            )
            voice_client.listen(STTAudioSink(self._processor, channel))
            logger.info("Joined voice channel: %s", channel)
        except Exception as exc:
            logger.error("Could not join %s: %s", channel, exc)

    async def _disconnect(self, voice_client: discord.VoiceClient | None) -> None:
        if voice_client is None:
            return

        self._processor.flush_all("leaving channel")
        await self._processor.drain()

        if voice_client.is_listening():
            voice_client.stop_listening()
        await voice_client.disconnect()

    # ── commands ──────────────────────────────────

    def _register_commands(self) -> None:
        bot = self._bot

        @bot.command(name="join")
        async def cmd_join(ctx: commands.Context) -> None:
            """Join the author's voice channel and start listening."""
            if not ctx.author.voice:
                await ctx.send("❌ You are not in a voice channel.")
                return

            channel = ctx.author.voice.channel

            if ctx.voice_client is not None:
                await ctx.voice_client.move_to(channel)
            else:
                await self._connect(channel)

            await ctx.send(f"🎙️ Joined **{channel}** — listening.")

        @bot.command(name="leave")
        async def cmd_leave(ctx: commands.Context) -> None:
            """Leave the current voice channel."""
            if not ctx.voice_client:
                await ctx.send("❌ I am not in a voice channel.")
                return

            await self._disconnect(ctx.voice_client)
            await ctx.send("👋 Left the voice channel.")
            logger.info("Left voice channel.")
