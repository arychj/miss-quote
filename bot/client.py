"""
Discord bot client setup, slash/prefix commands, and result handling.
"""

from __future__ import annotations

import asyncio
import json
import queue
from multiprocessing import Queue as MPQueue

import discord
import discord.ext.voice_recv
from discord.ext import commands

from bot.audio_sink import STTAudioSink
from config import discord_cfg, process_cfg
from utils.logging import get_logger

logger = get_logger(__name__)


class STTBot:
    """Wraps a commands.Bot with STT-specific lifecycle management."""

    def __init__(
        self,
        audio_queue: MPQueue,
        result_queue: MPQueue,
        command_queue: MPQueue,
    ) -> None:
        self._audio_q = audio_queue
        self._result_q = result_queue
        self._cmd_q = command_queue
        self._result_task: asyncio.Task | None = None

        intents = discord.Intents.default()
        intents.message_content = True

        self._bot = commands.Bot(
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
            if self._result_task is None or self._result_task.done():
                self._result_task = bot.loop.create_task(self._poll_results())

        @bot.event
        async def on_voice_state_update(
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
        ) -> None:
            if before.channel == after.channel:
                return

            if bot.user and member.id == bot.user.id and before.channel:
                self._send_command("FLUSH_ALL", None)
                return

            # Notify STT process when a user leaves or switches channels.
            if before.channel:
                self._send_command("LEAVE", member.id)

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
                vc = await channel.connect(
                    cls=discord.ext.voice_recv.VoiceRecvClient,
                )
                vc.listen(STTAudioSink(self._audio_q))

            await ctx.send(f"🎙️ Joined **{channel}** — listening.")
            logger.info("Joined voice channel: %s", channel)

        @bot.command(name="leave")
        async def cmd_leave(ctx: commands.Context) -> None:
            """Leave the current voice channel."""
            if ctx.voice_client:
                self._send_command("FLUSH_ALL", None)
                await ctx.voice_client.disconnect()
                await ctx.send("👋 Left the voice channel.")
                logger.info("Left voice channel.")
            else:
                await ctx.send("❌ I am not in a voice channel.")

    def _send_command(self, command: str, payload: object) -> None:
        try:
            self._cmd_q.put_nowait((command, payload))
        except queue.Full:
            logger.warning("Command queue is full; dropped %s command.", command)
        except Exception as exc:
            logger.error("Failed to send %s command: %s", command, exc)

    # ── result polling ────────────────────────────

    async def _poll_results(self) -> None:
        """Async loop that drains the result queue and logs transcriptions."""
        logger.info("Result polling task started.")
        while True:
            try:
                drained = False
                while True:
                    try:
                        result = self._result_q.get_nowait()
                    except queue.Empty:
                        break
                    drained = True
                    logger.info(
                        "📝 [User %s] %s  (latency %s)",
                        result["user_id"],
                        result["text"],
                        result["latency"],
                    )
                    # Pretty-print to console as well
                    print(json.dumps(result, ensure_ascii=False))

                if drained:
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(process_cfg.result_poll_interval)
            except Exception as exc:
                logger.error("Result poll error: %s", exc)
                await asyncio.sleep(1)
