"""
Discord bot client setup, voice lifecycle, and command handling.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from enum import Enum
from typing import Any

import discord
import discord.ext.voice_recv
from discord.ext import commands

from miss_quote.bot.announcer import DiscordAnnouncer
from miss_quote.bot.audio_sink import STTAudioSink
from miss_quote.bot.presence import DiscordPresence
from miss_quote.bot.speaker import DiscordSpeaker
from miss_quote.bot.topic import DiscordTopic
from miss_quote.config import (
    MONITORED_CHANNELS_KEY,
    discord_cfg,
    file_cfg,
    presence_cfg,
    transcript_cfg,
)
from miss_quote.stt.processor import STTProcessor
from miss_quote.tools.runner import ToolRunner
from miss_quote.transcript.writer import Source, TranscriptSession, TranscriptWriter
from miss_quote.utils.logging import get_logger
from miss_quote.utils.slugs import slugify

logger = get_logger(__name__)

# How often to check that a connected voice client is still receiving audio.
LISTEN_WATCHDOG_INTERVAL_SECONDS = 15.0

UNKNOWN_NAME = "unknown"
UNKNOWN_ID = 0

# Starting and stopping by hand, for a conversation the schedule did not cover
# or one it did and nobody wanted kept. Administrator-only: what these decide is
# whether everybody in the room is on the record.
START_TRANSCRIBING_COMMAND = "start-transcribing"
STOP_TRANSCRIBING_COMMAND = "stop-transcribing"

NOT_IN_A_CHANNEL = "❌ I am not in a voice channel here."


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
        self._topic = DiscordTopic(self._guild)
        self._announcer = DiscordAnnouncer(self._guild)
        self._tools = ToolRunner(
            speaker=self._speaker, topic=self._topic, announcer=self._announcer
        )
        self._processor = STTProcessor(self._tools)
        self._sessions: dict[int, TranscriptSession] = {}
        self._expiries: dict[int, asyncio.Task] = {}

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
        # After the bot, which it sets the presence of. Nothing hands this to a
        # tool, so unlike the speaker and the topic it has no ordering to work
        # around and can hold the client directly.
        self._presence = DiscordPresence(self._bot, presence_cfg.transcribing)

        self._watchdog: asyncio.Task | None = None
        self._prewarm: asyncio.Task | None = None
        self._services: Sequence[asyncio.Task] = ()

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
            self._report_schedule()
            self._report_tools()
            self._processor.start(asyncio.get_running_loop())
            self._start_listen_watchdog()
            self._start_prewarm()
            self._start_tools()
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

    @staticmethod
    def _report_schedule() -> None:
        """
        Say which rooms are on the record and when, room by room.

        Every one of them, not only the ones that named a window: being listed
        in `monitored_channels` is what puts a room on the record at all, and a
        deployment reading this wants to see the whole list rather than work out
        the absences. A room that is listed and covers nothing is an error, that
        being a schedule somebody wrote and nothing could be read out of.

        Nothing is said about the rooms that are not listed, there being no end
        to them; what they get is said once, here, in the line about the rest.
        """
        schedules = file_cfg.channel_schedules

        if not schedules:
            logger.warning(
                "No voice channel is listed in any server's '%s', so nothing "
                "will be written down. List the rooms that should be.",
                MONITORED_CHANNELS_KEY,
            )
            return

        # Sorted by what the line will say rather than by server ID, so the
        # report reads in the order somebody scanning it expects.
        listed = sorted(
            (
                (f"{file_cfg.alias_for(server_id) or server_id}/{channel}", schedule)
                for (server_id, channel), schedule in schedules.items()
            ),
            key=lambda entry: entry[0],
        )

        for where, schedule in listed:
            if schedule.empty:
                logger.error(
                    "Nothing in the schedule for %s could be read, so it will not "
                    "be written down. Correct it, or remove it to keep every "
                    "session in that room.",
                    where,
                )
            elif schedule.configured:
                logger.info("Keeping %s for sessions opening during: %s.", where, schedule.describe())
            else:
                logger.info("Keeping %s for every session.", where)

        logger.info(
            "Every other voice channel is transcribed and answered while the bot "
            "is in it, and nothing is written down. A session that opens on the "
            "record runs until the channel empties, however late."
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

    def _start_tools(self) -> None:
        """
        Set going every tool that has something of its own to do.

        Registered as background tasks, which is what gets them cancelled before
        the tools are asked to close: a tool writing itself out while its own
        loop is still going would be racing itself for the file.

        Once per process, however many readies the gateway sends. The runner
        starts them once either way; what a second pass would add is the same
        tasks in the cancellation list twice.
        """
        if self._services:
            return

        self._services = self._tools.start()
        self._bot.background_tasks.extend(self._services)

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
            return

        await self._refresh_presence()

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
        await self._refresh_presence()

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

    async def _refresh_presence(self) -> None:
        """
        Say whether anything is being kept, after anything that could change it.

        Derived from the open sessions rather than tracked alongside them, so
        there is one answer and no second copy of it to fall out of step. The
        presence deduplicates, so calling this after a transition that changed
        nothing costs nothing.

        A suspended session still counts. It is held open for a reconnect and
        will be appended to if one comes, so a transcript that is still going to
        take more of the conversation is one the room should still be told
        about; the alternative flickers the status through every resume window.
        """
        await self._presence.transcribing(
            any(session.capturing for session in self._sessions.values())
        )

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
            await self._refresh_presence()
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
        await self._refresh_presence()

    def _cancel_expiry(self, channel_id: int) -> bool:
        """Stop a pending seal, reporting whether there was one."""
        task = self._expiries.pop(channel_id, None)
        if task is None:
            return False

        task.cancel()
        return True

    async def _finalize(self, session: TranscriptSession) -> None:
        """
        Seal a transcript and hand it to the tools that want one.

        A session nobody spoke in has taken its own file away by this point and
        there is nothing to hand anybody. A tool given one could only find that
        out by reading it, and every tool would have to.
        """
        transcript = session.close()

        if transcript.empty:
            logger.info("Transcript discarded: %s (nobody spoke).", transcript.path)
            return

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

        await self._refresh_presence()

    # ── shutdown ──────────────────────────────────

    async def _shutdown(self) -> None:
        """
        Seal the transcripts and let the tools finish, before the loop goes away.

        The tools last, and after their own tasks have been cancelled: a tool
        handed a finished transcript may well have something new to write down,
        and closing it before that arrived would lose it.
        """
        await self._close_all_sessions()
        await self._tools.close()

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

        @bot.command(name=START_TRANSCRIBING_COMMAND)
        @commands.has_permissions(administrator=True)
        async def cmd_start_transcribing(ctx: commands.Context) -> None:
            """Put the session in the author's guild on the record."""
            session = self._session_in(ctx)
            if session is None:
                await ctx.send(NOT_IN_A_CHANNEL)
                return

            if not session.start_capturing():
                await ctx.send("📝 Already transcribing this conversation.")
                return

            await self._refresh_presence()
            await ctx.send(
                "📝 Transcribing from here on. Nothing said before this was kept."
            )
            logger.info(
                "Transcription started by hand in %s.", session.source.relative_directory
            )

        @bot.command(name=STOP_TRANSCRIBING_COMMAND)
        @commands.has_permissions(administrator=True)
        async def cmd_stop_transcribing(ctx: commands.Context) -> None:
            """Take the session in the author's guild off the record."""
            session = self._session_in(ctx)
            if session is None:
                await ctx.send(NOT_IN_A_CHANNEL)
                return

            if not session.stop_capturing():
                await ctx.send("🙊 Not transcribing this conversation.")
                return

            await self._refresh_presence()
            await ctx.send("🙊 Stopped transcribing. What was already written stays.")
            logger.info(
                "Transcription stopped by hand in %s.", session.source.relative_directory
            )

        for command in (cmd_start_transcribing, cmd_stop_transcribing):
            command.error(self._refuse_without_permission)

    def _session_in(self, ctx: commands.Context) -> TranscriptSession | None:
        """
        The open session for the channel the bot is in, in the asker's server.

        Through the voice client rather than the author's own channel: the bot
        holds one voice connection per server, and the session being asked about
        is the one it is sitting in, whether or not whoever typed this is there.
        """
        channel = getattr(ctx.voice_client, "channel", None)
        if channel is None:
            return None

        return self._sessions.get(getattr(channel, "id", UNKNOWN_ID))

    @staticmethod
    async def _refuse_without_permission(ctx: commands.Context, error: Exception) -> None:
        """
        Say no out loud, for the one refusal these commands can produce.

        A command that does nothing and says nothing is one somebody keeps
        trying. Anything else is re-raised rather than swallowed, so a real
        failure still reaches the log it would have reached.
        """
        if not isinstance(error, commands.MissingPermissions):
            raise error

        await ctx.send(
            f"❌ `{ctx.prefix}{ctx.invoked_with}` needs Administrator on this server."
        )
