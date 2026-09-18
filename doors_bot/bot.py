from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("doors-bot")


COOLDOWN_SECONDS = 4.0
THREAT_SECONDS = 5.0
QTE_SECONDS = 4.0
LOCKED_DOORS = {14, 22, 37, 60, 73}
SEEK_DOORS = {30, 80}
COIN_RANGE = (10, 50)


def clock() -> float:
    return time.monotonic()


def cancel_task(task: Optional[asyncio.Task[object]]) -> None:
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


@dataclass
class ThreatState:
    kind: str
    required_closet_uses: int = 1
    closet_uses: int = 0
    task: Optional[asyncio.Task[object]] = field(default=None, repr=False)


@dataclass
class DupeChallenge:
    correct_door: int
    wrong_door: int


@dataclass
class SeekChallenge:
    sequence: list[str]
    index: int = 0
    task: Optional[asyncio.Task[object]] = field(default=None, repr=False)
    message: Optional[discord.Message] = field(default=None, repr=False)


@dataclass
class HeartbeatChallenge:
    sequence: list[str]
    index: int = 0
    task: Optional[asyncio.Task[object]] = field(default=None, repr=False)
    message: Optional[discord.Message] = field(default=None, repr=False)


@dataclass
class GameSession:
    guild_id: int
    user_id: int
    current_door: int = 1
    health: int = 100
    wallet: int = 0
    coins_amount: int = 0
    coins_message: Optional[discord.Message] = field(default=None, repr=False)
    inventory: set[str] = field(default_factory=set)
    last_next_at: float = 0.0
    locked: bool = False
    looking_around: bool = False
    key_found: bool = False
    threat: Optional[ThreatState] = None
    dupe: Optional[DupeChallenge] = None
    seek: Optional[SeekChallenge] = None
    in_library: bool = False
    library_code: Optional[str] = None
    books_found: int = 0
    searching_book: bool = False
    heartbeat: Optional[HeartbeatChallenge] = None
    dead: bool = False
    escaped: bool = False
    last_channel: Optional[discord.abc.Messageable] = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    look_task: Optional[asyncio.Task[object]] = field(default=None, repr=False)

    @property
    def busy(self) -> bool:
        return any((self.threat, self.dupe, self.seek, self.heartbeat))


class GameButtonView(discord.ui.View):
    """Base view that keeps button interactions tied to one player."""

    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.session = session

    async def reject_other_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message(
                "This challenge belongs to another player.",
                ephemeral=True,
            )
            return True
        if self.session.dead or self.session.escaped:
            await interaction.response.send_message(
                "This run is no longer active.",
                ephemeral=True,
            )
            return True
        return False


class SeekView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session)
        self.add_item(self.DirectionButton("LEFT", "⬅️"))
        self.add_item(self.DirectionButton("RIGHT", "➡️"))
        self.add_item(self.DirectionButton("CROUCH", "🫥"))

    class DirectionButton(discord.ui.Button["SeekView"]):
        def __init__(self, action: str, emoji: str) -> None:
            super().__init__(
                label=action,
                emoji=emoji,
                style=discord.ButtonStyle.danger,
                custom_id=f"doors_seek_{action.lower()}",
            )
            self.action = action

        async def callback(self, interaction: discord.Interaction) -> None:
            await self.view.handle_action(interaction, self.action)

    async def handle_action(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        if await self.reject_other_user(interaction):
            return
        session = self.session
        async with session.lock:
            challenge = session.seek
            if not challenge:
                await interaction.response.send_message(
                    "The chase is already over.", ephemeral=True
                )
                return
            expected = challenge.sequence[challenge.index]
            if action != expected:
                await interaction.response.defer()
                await self.bot.kill_session(
                    session,
                    f"❌ You moved {action.lower()}, but the guiding light went "
                    f"{expected.lower()}. Seek catches you.",
                )
                return

            challenge.index += 1
            cancel_task(challenge.task)
            if challenge.index >= len(challenge.sequence):
                session.seek = None
                self.stop()
                await interaction.response.edit_message(
                    content=(
                        "✅ You follow the guiding light and lose Seek. "
                        "The corridor opens into the next room."
                    ),
                    view=None,
                )
                return

            challenge.task = asyncio.create_task(
                self.bot.seek_timeout(session, challenge.index)
            )
            remaining = " → ".join(challenge.sequence[challenge.index :])
            await interaction.response.edit_message(
                content=(
                    "👁️ SEEK CHASE\n"
                    f"Correct. Next movement: **{challenge.sequence[challenge.index]}**\n"
                    f"Sequence remaining: `{remaining}`\n"
                    f"You have {QTE_SECONDS:.0f} seconds."
                ),
                view=self,
            )


class HeartbeatView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session)
        buttons = (
            ("RED", "🔴"),
            ("BLUE", "🔵"),
            ("GREEN", "🟢"),
        )
        for action, emoji in buttons:
            self.add_item(self.ColorButton(action, emoji))

    class ColorButton(discord.ui.Button["HeartbeatView"]):
        def __init__(self, action: str, emoji: str) -> None:
            super().__init__(
                label=action,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"doors_heartbeat_{action.lower()}",
            )
            self.action = action

        async def callback(self, interaction: discord.Interaction) -> None:
            await self.view.handle_action(interaction, self.action)

    async def handle_action(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        if await self.reject_other_user(interaction):
            return
        session = self.session
        async with session.lock:
            challenge = session.heartbeat
            if not challenge:
                await interaction.response.send_message(
                    "The heartbeat pattern is already over.", ephemeral=True
                )
                return
            expected = challenge.sequence[challenge.index]
            if action != expected:
                await interaction.response.defer()
                await self.bot.kill_session(
                    session,
                    "💀 Your heartbeat breaks the pattern. Figure hears you.",
                )
                return

            challenge.index += 1
            cancel_task(challenge.task)
            if challenge.index >= len(challenge.sequence):
                session.heartbeat = None
                session.books_found += 1
                self.stop()
                fragment = session.library_code[session.books_found - 1]
                await interaction.response.edit_message(
                    content=(
                        "✅ The heartbeat fades. Figure moves away.\n"
                        f"📖 Code fragment {session.books_found}/5: "
                        f"digit **{session.books_found} = {fragment}**"
                    ),
                    view=None,
                )
                return

            challenge.task = asyncio.create_task(
                self.bot.heartbeat_timeout(session, challenge.index)
            )
            remaining = " ".join(
                self.bot.heartbeat_emoji[color]
                for color in challenge.sequence[challenge.index :]
            )
            await interaction.response.edit_message(
                content=(
                    "💓 HEARTBEAT\n"
                    f"Correct. Match the next color: "
                    f"**{challenge.sequence[challenge.index]}**\n"
                    f"Remaining: {remaining}\n"
                    f"You have {QTE_SECONDS:.0f} seconds."
                ),
                view=self,
            )


class DoorsBot(commands.Bot):
    def __init__(self) -> None:
        # Default intents include normal guild state but do not require the
        # privileged message-content intent because this bot is slash-command
        # and component based.
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        # Guild sessions use the guild ID; DM sessions use scope ID 0.
        self.sessions: dict[tuple[int, int], GameSession] = {}
        self.heartbeat_emoji = {
            "RED": "🔴",
            "BLUE": "🔵",
            "GREEN": "🟢",
        }
        guild_id = os.getenv("DISCORD_GUILD_ID")
        self.sync_guild_id = int(guild_id) if guild_id else None
        self.synced = False

    async def setup_hook(self) -> None:
        self.tree.add_command(self.start_command)
        self.tree.add_command(self.next_command)
        self.tree.add_command(self.look_around_command)
        self.tree.add_command(self.collect_command)
        self.tree.add_command(self.closet_command)
        self.tree.add_command(self.door_command)
        self.tree.add_command(self.search_book_command)
        self.tree.add_command(self.crack_code_command)
        self.tree.add_command(self.status_command)
        self.tree.add_command(self.crouch_command)
        self.tree.add_command(self.left_command)
        self.tree.add_command(self.right_command)
        self.tree.add_command(self.doors_help_command)
        self.tree.add_command(self.reset_command)

    async def on_ready(self) -> None:
        if not self.synced:
            # DMs only receive global application commands. Clear any old
            # server-scoped copies first, then sync the single global set.
            for guild in self.guilds:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            synced = await self.tree.sync()
            logger.info("Synced %d global slash commands for servers and DMs", len(synced))
            self.synced = True
        logger.info("Logged in as %s", self.user)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.error("Slash command failed: %s", error, exc_info=error)
        if isinstance(error, app_commands.CommandSignatureMismatch):
            # A command interaction may have been opened from Discord's stale
            # global cache. Refresh the current guild copy and tell the player
            # to retry instead of letting the three-second interaction window
            # expire with no response.
            await self.tree.sync()
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "The command list was refreshed. Please run that command again.",
                    ephemeral=True,
                )

    def session_for(self, interaction: discord.Interaction) -> Optional[GameSession]:
        scope_id = interaction.guild_id or 0
        return self.sessions.get((scope_id, interaction.user.id))

    async def require_session(
        self, interaction: discord.Interaction
    ) -> Optional[GameSession]:
        session = self.session_for(interaction)
        if session is None:
            await interaction.response.send_message(
                "You do not have an active run. Use `/start` first.",
                ephemeral=True,
            )
            return None
        if session.dead:
            await interaction.response.send_message(
                "Your run ended. Use `/start` to begin a new attempt.",
                ephemeral=True,
            )
            return None
        if session.escaped:
            await interaction.response.send_message(
                "You already escaped Floor 1. Use `/start` to replay.",
                ephemeral=True,
            )
            return None
        session.last_channel = interaction.channel
        return session

    async def send_room_entry(self, session: GameSession) -> None:
        channel = session.last_channel
        if channel is None:
            return
        door = session.current_door
        session.coins_amount = random.randint(*COIN_RANGE)
        session.coins_message = await channel.send(
            f"🚪 **Opened Door {door}**\n"
            f"🪙 **Coins found: {session.coins_amount}** "
            f"(Collect using `/collect`)"
        )

        if door in LOCKED_DOORS:
            session.locked = True
            session.key_found = False
            await channel.send(
                "🔒 **Status: Locked.**\n"
                "The door is padlocked. You need a key. "
                "Use `/look_around` to search the drawers."
            )
        elif door >= 10:
            await self.roll_room_event(session)

    async def roll_room_event(self, session: GameSession) -> None:
        channel = session.last_channel
        if channel is None:
            return
        roll = random.randint(1, 100)
        if roll <= 10:
            await self.start_threat(session, "ambush")
        elif roll <= 30:
            await self.start_threat(session, "rush")
        else:
            await channel.send("The room is quiet. For now.")

    async def start_threat(self, session: GameSession, kind: str) -> None:
        channel = session.last_channel
        if channel is None:
            return
        required = random.randint(2, 6) if kind == "ambush" else 1
        threat = ThreatState(kind=kind, required_closet_uses=required)
        session.threat = threat
        if kind == "ambush":
            prompt = (
                "⚠️ **AMBUSH APPROACHING — 5 SECONDS!**\n"
                "Hide in a closet, then exit and re-enter when Ambush rebounds.\n"
                f"You must complete **{required} closet entries**. Use `/closet` now!"
            )
        else:
            prompt = (
                "⚠️ **ALERT: RUSH arrives in 5 seconds!**\n"
                "🚪 Available Closets: 4\n"
                "Quick! Hide using `/closet`!"
            )
        await channel.send(prompt)
        threat.task = asyncio.create_task(self.threat_timeout(session))

    async def threat_timeout(self, session: GameSession) -> None:
        try:
            await asyncio.sleep(THREAT_SECONDS)
            async with session.lock:
                if session.threat:
                    await self.kill_session(
                        session,
                        "💀 You were too slow. The entity found you.",
                    )
        except asyncio.CancelledError:
            return

    async def seek_timeout(self, session: GameSession, expected_index: int) -> None:
        try:
            await asyncio.sleep(QTE_SECONDS)
            async with session.lock:
                if session.seek and session.seek.index == expected_index:
                    await self.kill_session(
                        session,
                        "💀 You hesitate for too long. Seek catches you.",
                    )
        except asyncio.CancelledError:
            return

    async def heartbeat_timeout(
        self, session: GameSession, expected_index: int
    ) -> None:
        try:
            await asyncio.sleep(QTE_SECONDS)
            async with session.lock:
                if session.heartbeat and session.heartbeat.index == expected_index:
                    await self.kill_session(
                        session,
                        "💀 You miss the heartbeat window. Figure hears you.",
                    )
        except asyncio.CancelledError:
            return

    async def kill_session(self, session: GameSession, reason: str) -> None:
        session.dead = True
        cancel_task(session.threat.task if session.threat else None)
        cancel_task(session.seek.task if session.seek else None)
        cancel_task(session.heartbeat.task if session.heartbeat else None)
        cancel_task(session.look_task)
        session.threat = None
        session.seek = None
        session.heartbeat = None
        session.look_task = None
        channel = session.last_channel
        if channel:
            await channel.send(
                f"{reason}\n\n"
                f"**Run over at Door {session.current_door}.** "
                "Use `/start` to try again."
            )

    async def submit_seek_command(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if not session.seek:
                await interaction.response.send_message(
                    "You are not in a Seek chase.", ephemeral=True
                )
                return
            expected = session.seek.sequence[session.seek.index]
            if action != expected:
                await interaction.response.defer()
                await self.kill_session(
                    session,
                    f"❌ You chose {action.lower()} instead of "
                    f"{expected.lower()}. Seek catches you.",
                )
                return
            session.seek.index += 1
            cancel_task(session.seek.task)
            if session.seek.index >= len(session.seek.sequence):
                session.seek = None
                await interaction.response.send_message(
                    "✅ You lose Seek in the next room.", ephemeral=False
                )
                return
            session.seek.task = asyncio.create_task(
                self.seek_timeout(session, session.seek.index)
            )
            await interaction.response.send_message(
                f"✅ Correct. Next: **{session.seek.sequence[session.seek.index]}** "
                f"({QTE_SECONDS:.0f}s)",
                ephemeral=False,
            )

    @app_commands.command(name="start", description="Start a new DOORS Floor 1 run.")
    async def start_command(self, interaction: discord.Interaction) -> None:
        key = (interaction.guild_id or 0, interaction.user.id)
        old = self.sessions.get(key)
        if old:
            cancel_task(old.threat.task if old.threat else None)
            cancel_task(old.seek.task if old.seek else None)
            cancel_task(old.heartbeat.task if old.heartbeat else None)
            cancel_task(old.look_task)
        session = GameSession(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            last_channel=interaction.channel,
        )
        self.sessions[key] = session
        await interaction.response.send_message(
            "🚪 **DOORS — Floor 1**\n"
            "You enter at Door 1. Reach Door 100 alive.\n"
            "Use `/next` to move. Use `/doors_help` if you need the command list.\n"
            "Your run is private to this conversation."
        )

    @app_commands.command(name="next", description="Advance 1–3 doors.")
    @app_commands.describe(amount="How many doors to advance (1–3).")
    async def next_command(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 3] = 1
    ) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if session.busy or session.looking_around or session.searching_book:
                await interaction.response.send_message(
                    "Finish the active encounter before moving.", ephemeral=True
                )
                return
            now = clock()
            elapsed = now - session.last_next_at
            if elapsed < COOLDOWN_SECONDS:
                await interaction.response.send_message(
                    f"⏳ The doors need a moment. Try again in "
                    f"{COOLDOWN_SECONDS - elapsed:.1f}s.",
                    ephemeral=True,
                )
                return
            session.last_next_at = now

            if session.locked and not session.key_found:
                await interaction.response.send_message(
                    "🔒 This door is locked. Use `/look_around` to search for the key.",
                    ephemeral=True,
                )
                return
            if session.locked:
                session.locked = False
                session.key_found = False
                session.inventory.discard("room_key")

            target = session.current_door + amount
            if target > 100:
                session.current_door = 100
                session.escaped = True
                await interaction.response.send_message(
                    "🏃 **You reach the elevator and escape Floor 1.**\n"
                    f"Coins collected: **{session.wallet}**",
                )
                return

            if amount == 3 and session.current_door >= 10 and random.random() < 0.35:
                correct = target
                session.dupe = DupeChallenge(correct_door=correct, wrong_door=correct - 1)
                await interaction.response.send_message(
                    "🚪 **DUPE HALLWAY**\n"
                    "The previous room log goes dark. Two doors stand before you.\n"
                    f"One is numbered **{correct}**, the other **{correct - 1}**.\n"
                    "Use `/door number` to choose. The wrong door deals **40 damage**.",
                )
                return

            session.current_door = target
            if session.current_door == 50:
                session.in_library = True
                session.library_code = "".join(str(random.randint(0, 9)) for _ in range(5))
                await interaction.response.send_message(
                    "📚 **Door 50: The Library**\n"
                    "The room is massive, dark, and filled with bookshelves. "
                    "Figure is patrolling the center. It cannot see, but it can hear.\n"
                    "📖 **Books collected: 0/5**\n"
                    "Use `/search_book` carefully. Use `/crouch` if footsteps approach.",
                )
                return

            if session.current_door in SEEK_DOORS:
                await interaction.response.defer()
                await self.start_seek(session)
                return

            await interaction.response.defer()
            await self.send_room_entry(session)

    async def start_seek(self, session: GameSession) -> None:
        channel = session.last_channel
        if channel is None:
            return
        sequence = [random.choice(["LEFT", "RIGHT", "CROUCH"]) for _ in range(5)]
        challenge = SeekChallenge(sequence=sequence)
        session.seek = challenge
        challenge.task = asyncio.create_task(self.seek_timeout(session, 0))
        challenge.message = await channel.send(
            "👁️ **SEEK CHASE INITIATED**\n"
            "You are running down a long corridor. Seek is right behind you!\n"
            f"🔵 Guiding Light: **{sequence[0]}**\n"
            "Use the buttons below in the next 4 seconds.",
            view=SeekView(self, session),
        )

    @app_commands.command(
        name="look_around", description="Search a locked room for its key."
    )
    async def look_around_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if not session.locked:
                await interaction.response.send_message(
                    "There is no locked door to search here.", ephemeral=True
                )
                return
            if session.key_found:
                await interaction.response.send_message(
                    "🔑 You already found the key. Use `/next 1` to unlock the door.",
                    ephemeral=True,
                )
                return
            if session.looking_around:
                await interaction.response.send_message(
                    "You are still searching the drawers...", ephemeral=True
                )
                return
            session.looking_around = True
            await interaction.response.send_message(
                "🔎 You search the drawers and furniture... "
                "This will take 5–7 seconds."
            )
        try:
            await asyncio.sleep(random.randint(5, 7))
            async with session.lock:
                if session.dead or not session.locked:
                    return
                session.key_found = True
                session.inventory.add("room_key")
                await interaction.channel.send(
                    f"🔑 **Item Found:** You found the Room {session.current_door + 1} "
                    "Key inside a plant pot! Use `/next 1` to unlock and proceed."
                )
        finally:
            session.looking_around = False

    @app_commands.command(name="collect", description="Collect coins in the current room.")
    async def collect_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if session.coins_amount <= 0:
                await interaction.response.send_message(
                    "There are no coins left in this room.", ephemeral=True
                )
                return
            amount = session.coins_amount
            session.wallet += amount
            session.coins_amount = 0
            message = session.coins_message
            session.coins_message = None
            if message:
                try:
                    await message.delete()
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    logger.warning("Cannot delete coin message in channel %s", message.channel.id)
            await interaction.response.send_message(
                f"🪙 You collect **{amount} coins**. Wallet: **{session.wallet}**."
            )

    @app_commands.command(name="closet", description="Hide from Rush or Ambush.")
    async def closet_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if not session.threat:
                await interaction.response.send_message(
                    "There is no active entity to hide from.", ephemeral=True
                )
                return
            threat = session.threat
            threat.closet_uses += 1
            if threat.kind == "rush":
                cancel_task(threat.task)
                session.threat = None
                await interaction.response.send_message(
                    "🚪 You slam the closet shut. Rush tears past, then vanishes."
                )
                return
            if threat.closet_uses >= threat.required_closet_uses:
                cancel_task(threat.task)
                session.threat = None
                await interaction.response.send_message(
                    "🚪 You exit and re-enter the closet at exactly the right moment. "
                    "Ambush finally gives up."
                )
                return
            await interaction.response.send_message(
                f"🚪 Ambush rebounds! Exit and re-enter the closet. "
                f"Progress: **{threat.closet_uses}/{threat.required_closet_uses}**"
            )

    @app_commands.command(name="door", description="Choose a number in a Dupe hallway.")
    @app_commands.describe(number="The number written on the door you choose.")
    async def door_command(self, interaction: discord.Interaction, number: int) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if not session.dupe:
                await interaction.response.send_message(
                    "There is no Dupe hallway right now.", ephemeral=True
                )
                return
            challenge = session.dupe
            session.dupe = None
            if number != challenge.correct_door:
                session.health -= 40
                if session.health <= 0:
                    await interaction.response.defer()
                    await self.kill_session(
                        session, "💀 You choose the wrong door. Dupe tears you apart."
                    )
                    return
                await interaction.response.send_message(
                    f"❌ Wrong door. Dupe deals **40 damage**. Health: **{session.health}/100**.\n"
                    f"The correct door was {challenge.correct_door}. Use `/next 1` to continue."
                )
                return
            session.current_door = challenge.correct_door
            await interaction.response.defer()
            await self.send_room_entry(session)

    @app_commands.command(
        name="search_book", description="Search the Library for a code fragment."
    )
    async def search_book_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if not session.in_library or session.current_door != 50:
                await interaction.response.send_message(
                    "You can only search books in Door 50's Library.", ephemeral=True
                )
                return
            if session.books_found >= 5:
                await interaction.response.send_message(
                    "You found all five fragments. Use `/crack_code`.", ephemeral=True
                )
                return
            if session.searching_book or session.heartbeat:
                await interaction.response.send_message(
                    "You are already focused on a book or heartbeat pattern.",
                    ephemeral=True,
                )
                return
            session.searching_book = True
            await interaction.response.defer()

        try:
            await asyncio.sleep(random.uniform(1.0, 2.0))
            async with session.lock:
                if session.dead:
                    return
                if random.random() < 0.30:
                    sequence = [random.choice(["RED", "BLUE", "GREEN"]) for _ in range(3)]
                    challenge = HeartbeatChallenge(sequence=sequence)
                    session.heartbeat = challenge
                    challenge.task = asyncio.create_task(
                        self.heartbeat_timeout(session, 0)
                    )
                    shown = " ".join(self.heartbeat_emoji[color] for color in sequence)
                    await interaction.followup.send(
                        "💓 **HEARTBEAT MINIGAME**\n"
                        "Figure is close. Match the colors in order before the next beat:\n"
                        f"{shown}\n"
                        f"First color: **{sequence[0]}** — you have {QTE_SECONDS:.0f} seconds.",
                        view=HeartbeatView(self, session),
                    )
                else:
                    session.books_found += 1
                    fragment = session.library_code[session.books_found - 1]
                    await interaction.followup.send(
                        f"📖 You find a hidden book. Code fragment "
                        f"**{session.books_found}/5**: digit "
                        f"**{session.books_found} = {fragment}**\n"
                        f"Books collected: **{session.books_found}/5**"
                    )
        finally:
            session.searching_book = False

    @app_commands.command(
        name="crack_code", description="Enter the five-digit Library escape code."
    )
    @app_commands.describe(code="The five-digit code built from the book fragments.")
    async def crack_code_command(
        self, interaction: discord.Interaction, code: str
    ) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if not session.in_library:
                await interaction.response.send_message(
                    "There is no lock code to crack here.", ephemeral=True
                )
                return
            if session.books_found < 5:
                await interaction.response.send_message(
                    f"You need all five code fragments first ({session.books_found}/5).",
                    ephemeral=True,
                )
                return
            if not code.isdigit() or len(code) != 5:
                await interaction.response.send_message(
                    "The Library code must be exactly five digits.", ephemeral=True
                )
                return
            if code != session.library_code:
                await interaction.response.defer()
                await self.kill_session(
                    session, "💀 The code is wrong. Figure finds you in the dark."
                )
                return
            session.current_door = 51
            session.in_library = False
            await interaction.response.send_message(
                "🔓 **Code accepted.** You slip through the Library exit into Door 51."
            )

    @app_commands.command(name="status", description="Show your current run state.")
    async def status_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            active = "None"
            if session.threat:
                active = session.threat.kind.title()
            elif session.dupe:
                active = "Dupe"
            elif session.seek:
                active = "Seek"
            elif session.heartbeat:
                active = "Heartbeat"
            await interaction.response.send_message(
                f"🚪 Door: **{session.current_door}**\n"
                f"❤️ Health: **{session.health}/100**\n"
                f"🪙 Wallet: **{session.wallet}**\n"
                f"📦 Inventory: **{', '.join(sorted(session.inventory)) or 'empty'}**\n"
                f"📖 Library books: **{session.books_found}/5**\n"
                f"⚠️ Active encounter: **{active}**",
                ephemeral=True,
            )

    @app_commands.command(name="crouch", description="Crouch quietly in the Library.")
    async def crouch_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        await interaction.response.send_message(
            "🫥 You crouch and let the footsteps pass.", ephemeral=False
        )

    @app_commands.command(name="left", description="Choose LEFT during a Seek chase.")
    async def left_command(self, interaction: discord.Interaction) -> None:
        await self.submit_seek_command(interaction, "LEFT")

    @app_commands.command(name="right", description="Choose RIGHT during a Seek chase.")
    async def right_command(self, interaction: discord.Interaction) -> None:
        await self.submit_seek_command(interaction, "RIGHT")

    @app_commands.command(
        name="doors_help", description="Show the DOORS command guide."
    )
    async def doors_help_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "📘 **DOORS Floor 1 commands**\n"
            "`/start` — begin or restart your run\n"
            "`/next 1-3` — advance, with a 4-second cooldown\n"
            "`/look_around` — search a locked room (5–7 seconds)\n"
            "`/collect` — collect room coins and remove the coin message\n"
            "`/closet` — survive Rush or complete Ambush's repeated entries\n"
            "`/door number` — choose the real door during Dupe\n"
            "`/search_book` — gather five Library fragments\n"
            "`/crack_code 12345` — escape the Library\n"
            "`/status` — inspect your run\n"
            "Seek uses buttons; `/left` and `/right` are fallback commands.",
            ephemeral=True,
        )

    @app_commands.command(name="reset", description="Delete your active run.")
    async def reset_command(self, interaction: discord.Interaction) -> None:
        key = (interaction.guild_id or 0, interaction.user.id)
        session = self.sessions.pop(key, None)
        if session:
            cancel_task(session.threat.task if session.threat else None)
            cancel_task(session.seek.task if session.seek else None)
            cancel_task(session.heartbeat.task if session.heartbeat else None)
            cancel_task(session.look_task)
        await interaction.response.send_message(
            "🧹 Your run has been reset. Use `/start` to begin again.",
            ephemeral=True,
        )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Add it to Replit Secrets or your shell environment."
        )
    bot = DoorsBot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()