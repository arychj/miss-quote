"""
Discord bot client setup, voice lifecycle, and command handling.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

import discord
import discord.ext.voice_recv
from discord.ext import commands

from miss_quote.bot.audio_sink import STTAudioSink
from miss_quote.bot.scoreboard import Scoreboard
from miss_quote.bot.speaker import DiscordSpeaker
from miss_quote.config import discord_cfg, file_cfg, morality_cfg, transcript_cfg
from miss_quote.ledger.credits import shared_ledger
from miss_quote.stt.processor import STTProcessor
from miss_quote.tools.runner import ToolRunner
from miss_quote.transcript.writer import Source, TranscriptSession, TranscriptWriter, slugify
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

# How often to check that a connected voice client is still receiving audio.
LISTEN_WATCHDOG_INTERVAL_SECONDS = 15.0

UNKNOWN_NAME = "unknown"
UNKNOWN_ID = 0


def source_for(channel: Any) -> Source:
    """
    Where a channel's transcripts belong.

    A session only exists for a server the bot was allowed to join, so the alias
    is configured; the Discord name is a fallback for nothing in particular
    going wrong.
    """
    guild = getattr(channel, "guild", None)
    guild_id = getattr(guild, "id", UNKNOWN_ID)

    return Source(
        guild_id=guild_id,
        guild_alias=file_cfg.alias_for(guild_id) or getattr(guild, "name", UNKNOWN_NAME),
        channel_id=getattr(channel, "id", UNKNOWN_ID),
        channel=getattr(channel, "name", UNKNOWN_NAME),
    )


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
    """A Bot that drains the processor and seals its transcripts before the loop goes away."""

    def __init__(
        self,
        processor: STTProcessor,
        on_close: Callable[[], Awaitable[None]],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._processor = processor
        self._on_close = on_close
        self.background_tasks: list[asyncio.Task] = []

    async def close(self) -> None:
        # SIGTERM lands here on pod termination; buffered speech is only lost if
        # it is not flushed before the loop stops.
        for task in self.background_tasks:
            task.cancel()

        await self._processor.stop()

        # Sessions close after the drain, so a tool handed a finished transcript
        # sees every utterance that made it to disk.
        await self._on_close()
        await super().close()


class STTBot:
    """Wraps a commands.Bot with voice-transcription lifecycle management."""

    def __init__(self) -> None:
        self._writer = TranscriptWriter()
        self._speaker = DiscordSpeaker(self._guild)
        self._tools = ToolRunner(speaker=self._speaker)
        self._processor = STTProcessor(self._tools)
        self._sessions: dict[int, TranscriptSession] = {}
        self._expiries: dict[int, asyncio.Task] = {}

        # After the tools, which are what enroll a server's roster in the tally.
        self._scoreboard = Scoreboard(shared_ledger(), self._guild)

        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True

        self._bot = _Bot(
            self._processor,
            self._shutdown,
            command_prefix=discord_cfg.command_prefix,
            intents=intents,
        )
        self._watchdog: asyncio.Task | None = None
        self._prewarm: asyncio.Task | None = None
        self._tally: asyncio.Task | None = None

        self._register_events()
        self._register_commands()

    def _guild(self, guild_id: int) -> discord.Guild | None:
        """
        Resolve a guild for the speaker.

        Handed over as a callable rather than the bot itself, because the
        speaker is built before the bot is: the bot is built around the
        processor, the processor around the tools, and the tools want a speaker.
        """
        return self._bot.get_guild(guild_id)

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
            self._report_servers()
            self._report_tools()
            self._processor.start(asyncio.get_running_loop())
            self._start_listen_watchdog()
            self._start_prewarm()
            self._start_scoreboard()
            if discord_cfg.autojoin:
                await self._join_active_channels()

        @bot.event
        async def on_voice_state_update(
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
        ) -> None:
            if bot.user is not None and member.id == bot.user.id:
                # Being removed from a channel by someone else ends the session
                # just as surely as leaving on purpose does.
                if before.channel and not after.channel:
                    self._processor.flush_all("bot disconnected")
                    await self._processor.drain()
                    await self._end_session(getattr(before.channel, "id", UNKNOWN_ID))
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

    def _report_servers(self) -> None:
        """
        Reconcile the configured servers against the ones the bot is actually in.

        Four things can be wrong and none of them raise: the file has entries
        that would not parse, nothing is configured, a server is configured but
        the bot was never invited, or the bot is in a server nobody configured.
        Each is reported so none of them has to be discovered by noticing an
        empty transcript directory.
        """
        for problem in file_cfg.problems:
            logger.error("%s: %s", file_cfg.path, problem)

        if not file_cfg.servers:
            logger.warning(
                "No servers are configured (%s %s); the bot will not join any voice channel.",
                file_cfg.path,
                "is empty" if file_cfg.found else "was not found",
            )
            return

        joined = {guild.id for guild in self._bot.guilds}
        aliases = [server.alias for server in file_cfg.servers.values()]

        logger.info(
            "Known servers: %s",
            ", ".join(
                f"{server.alias} ({'joined' if server_id in joined else 'not joined'})"
                for server_id, server in sorted(
                    file_cfg.servers.items(), key=lambda item: item[1].alias
                )
            ),
        )

        missing = [
            server.alias
            for server_id, server in file_cfg.servers.items()
            if server_id not in joined
        ]
        if missing:
            logger.warning(
                "Configured but not joined: %s. The bot needs an invite to each.",
                ", ".join(sorted(missing)),
            )

        duplicates = sorted(alias for alias in set(aliases) if aliases.count(alias) > 1)
        if duplicates:
            logger.error(
                "Alias reused by more than one server: %s. "
                "Their transcripts will be mixed together in one directory, with "
                "nothing to say which came from where.",
                ", ".join(duplicates),
            )

        unknown = [guild for guild in self._bot.guilds if not file_cfg.knows(guild.id)]
        if unknown:
            logger.warning(
                "In %d server(s) that are not configured, and will not join voice there: %s",
                len(unknown),
                ", ".join(f"{guild.name} ({guild.id})" for guild in unknown),
            )

    def _report_tools(self) -> None:
        """Say which tools are in play, and complain about the ones that are not."""
        for problem in self._tools.problems:
            logger.error("%s", problem)

        enabled = self._tools.describe()
        if not enabled:
            logger.info("No tools are enabled; transcripts are written and nothing reads them.")
            return

        logger.info(
            "Tools enabled: %s",
            "; ".join(
                f"{alias}: {', '.join(names)}" for alias, names in sorted(enabled.items())
            ),
        )

    def _start_listen_watchdog(self) -> None:
        if self._watchdog is not None and not self._watchdog.done():
            return

        self._watchdog = asyncio.create_task(self._watch_listening())
        self._bot.background_tasks.append(self._watchdog)

    async def _watch_listening(self) -> None:
        """
        Re-attach a sink when a connected voice client stops receiving.

        `PacketRouter` calls `stop_listening()` from its `finally`, so anything
        that kills that thread leaves the bot in the channel and deaf. Nothing
        in discord.py re-arms it, and the failure is silent.
        """
        while True:
            await asyncio.sleep(LISTEN_WATCHDOG_INTERVAL_SECONDS)

            try:
                for guild in self._bot.guilds:
                    voice_client = guild.voice_client
                    if voice_client is None or not voice_client.is_connected():
                        continue
                    if voice_client.is_listening():
                        continue

                    logger.warning(
                        "Voice receive stopped in '%s'; re-attaching the sink.",
                        voice_client.channel,
                    )
                    # The existing session is reused: the bot never left, so the
                    # transcript should not be split by an internal failure.
                    voice_client.listen(
                        STTAudioSink(
                            self._processor, self._session_for(voice_client.channel)
                        )
                    )
            except Exception as exc:
                logger.error("Listen watchdog error: %s", exc)

    def _start_prewarm(self) -> None:
        """
        Let the tools prepare what they can, out of the way of joining a channel.

        A background task rather than an await: rendering a phrase per speaker
        takes as long as the synthesizer takes, and the bot should be in the
        channel and listening while it happens.

        Once per process, however many readies the gateway sends. A second pass
        would synthesize nothing, every phrase having been cached by the first,
        but the scan and the line in the log are not free either.
        """
        if self._prewarm is not None:
            return

        self._prewarm = asyncio.create_task(self._tools.prewarm())
        self._bot.background_tasks.append(self._prewarm)

    def _start_scoreboard(self) -> None:
        """
        Start publishing the credit tally, if anything is counting.

        Nothing to publish is the ordinary case for a deployment that has enabled
        no tool that fines anybody, and a task waking on an interval to look at an
        empty ledger is a task worth not starting. A server enrolls its roster
        when its tools are built, which is before this runs.
        """
        if self._tally is not None or not self._ledger_worth_publishing():
            return

        self._tally = asyncio.create_task(self._scoreboard.run())
        self._bot.background_tasks.append(self._tally)

    @staticmethod
    def _ledger_worth_publishing() -> bool:
        if not morality_cfg.counting_enabled:
            logger.info(
                "CREDITS_SAVE_SECONDS is %s; the credit tally will be kept in memory "
                "and written only on shutdown.",
                morality_cfg.save_interval_seconds,
            )
            return False

        if not morality_cfg.publishing_enabled:
            logger.info(
                "CREDITS_TOPIC_SECONDS is %s; the credit tally will be kept and "
                "written, but never published to a channel topic.",
                morality_cfg.topic_interval_seconds,
            )

        return bool(shared_ledger().servers())

    async def _join_active_channels(self) -> None:
        """Pick up channels that already had people in them when the bot started."""
        for guild in self._bot.guilds:
            if guild.voice_client is not None or not file_cfg.knows(guild.id):
                continue
            for channel in guild.voice_channels:
                if humans_in(channel) > 0:
                    await self._connect(channel)
                    break

    # ── voice lifecycle ───────────────────────────

    async def _connect(self, channel: discord.abc.Connectable) -> None:
        guild = getattr(channel, "guild", None)

        if guild is None or not file_cfg.knows(guild.id):
            logger.warning(
                "Refusing to join '%s': server %s is not a known server.",
                channel,
                getattr(guild, "id", "unknown"),
            )
            return

        self._warn_on_colliding_channel(channel, guild)

        try:
            voice_client = await channel.connect(
                cls=discord.ext.voice_recv.VoiceRecvClient,
            )
            voice_client.listen(STTAudioSink(self._processor, self._session_for(channel)))
            logger.info("Joined voice channel: %s", channel)
        except Exception as exc:
            logger.error("Could not join %s: %s", channel, exc)

    @staticmethod
    def _warn_on_colliding_channel(channel: Any, guild: Any) -> None:
        """
        Warn when two voice channels would share a transcript directory.

        Paths carry no channel ID, so two channels whose names reduce to the
        same slug write to the same files. Discord permits that; nothing about
        the path can catch it.
        """
        slug = slugify(getattr(channel, "name", ""))
        clashing = [
            sibling.name
            for sibling in getattr(guild, "voice_channels", None) or []
            if sibling.id != channel.id and slugify(sibling.name) == slug
        ]

        if clashing:
            logger.error(
                "Voice channel '%s' shares the directory '%s' with: %s. "
                "Their transcripts will be mixed together, with nothing to say "
                "which came from where.",
                channel,
                slug,
                ", ".join(clashing),
            )

    async def _move(
        self, voice_client: discord.VoiceClient, channel: discord.abc.Connectable
    ) -> None:
        """
        Move to another channel, ending one transcript and starting another.

        A bot holds one voice connection per guild, so a move is a leave and a
        join. Carrying the session across would file one channel's speech under
        another channel's directory.
        """
        previous = getattr(voice_client.channel, "id", UNKNOWN_ID)

        self._processor.flush_all("moving channel")
        await self._processor.drain()
        if voice_client.is_listening():
            voice_client.stop_listening()

        await voice_client.move_to(channel)
        await self._end_session(previous)

        voice_client.listen(STTAudioSink(self._processor, self._session_for(channel)))

    async def _disconnect(self, voice_client: discord.VoiceClient | None) -> None:
        if voice_client is None:
            return

        channel_id = getattr(voice_client.channel, "id", UNKNOWN_ID)

        self._processor.flush_all("leaving channel")
        await self._processor.drain()

        if voice_client.is_listening():
            voice_client.stop_listening()
        await voice_client.disconnect()

        await self._end_session(channel_id)

    # ── transcript sessions ───────────────────────

    def _session_for(self, channel: Any) -> TranscriptSession:
        """
        The open session for a channel, opening one if there is none.

        Keyed by channel rather than guild so the lookup stays honest if a bot
        is ever able to hold two voice connections in one server.
        """
        channel_id = getattr(channel, "id", UNKNOWN_ID)

        session = self._sessions.get(channel_id)
        if session is not None:
            if self._cancel_expiry(channel_id):
                session.resume()
                logger.info("Resuming transcript %s.", session.path)
            return session

        session = self._writer.open(source_for(channel))
        self._sessions[channel_id] = session
        return session

    async def _end_session(self, channel_id: int) -> None:
        """
        Start the clock on a channel's transcript rather than sealing it.

        A channel that empties and refills inside the resume window is one
        conversation with a gap in it — someone's client dropped, or the last
        person stepped away — so the transcript is held open for it. Sealing on
        every disconnect would hand a tool a fragment, then hand it a second
        fragment containing the first one over again.
        """
        session = self._sessions.get(channel_id)
        if session is None:
            return

        self._cancel_expiry(channel_id)
        session.suspend()

        if not transcript_cfg.resume_enabled:
            del self._sessions[channel_id]
            await self._finalize(session)
            return

        self._expiries[channel_id] = asyncio.create_task(
            self._expire_session(channel_id, session)
        )

    async def _expire_session(self, channel_id: int, session: TranscriptSession) -> None:
        """Seal a session nobody came back for. Cancelled if they do."""
        await asyncio.sleep(transcript_cfg.resume_window_seconds)

        self._expiries.pop(channel_id, None)
        if self._sessions.get(channel_id) is session:
            del self._sessions[channel_id]

        await self._finalize(session)

    def _cancel_expiry(self, channel_id: int) -> bool:
        """Stop a pending seal, reporting whether there was one."""
        task = self._expiries.pop(channel_id, None)
        if task is None:
            return False

        task.cancel()
        return True

    async def _finalize(self, session: TranscriptSession) -> None:
        """Seal a transcript and hand it to the tools that want one."""
        transcript = session.close()
        logger.info(
            "Transcript closed: %s (%d utterance(s)).",
            transcript.path,
            transcript.utterances,
        )

        await self._tools.dispatch_finished(transcript)

    async def _close_all_sessions(self) -> None:
        """
        Seal everything now, resume window or not.

        This runs on the way to the loop stopping, so a session held open for a
        reconnect that will never come would be lost rather than delayed.
        """
        for channel_id in list(self._sessions):
            self._cancel_expiry(channel_id)
            await self._finalize(self._sessions.pop(channel_id))

    # ── shutdown ──────────────────────────────────

    async def _shutdown(self) -> None:
        """
        Seal the transcripts and write the tally down before the loop goes away.

        The tally is only saved here, not published: the interval task has been
        cancelled by now, and a channel edit that lands in a rate-limit bucket
        would sit on SIGTERM until the pod is killed outright. What matters at
        this point is the file, which is nobody's rate limit.
        """
        await self._close_all_sessions()
        await self._scoreboard.persist()

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
                await self._move(ctx.voice_client, channel)
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
