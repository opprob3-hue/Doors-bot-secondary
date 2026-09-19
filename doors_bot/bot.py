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
    hiding: bool = False
    hide_task: Optional[asyncio.Task[object]] = field(default=None, repr=False)
    eyes_active: bool = False
    eyes_task: Optional[asyncio.Task[object]] = field(default=None, repr=False)
    screech_active: bool = False
    screech_task: Optional[asyncio.Task[object]] = field(default=None, repr=False)
    halt_active: bool = False
    halt_task: Optional[asyncio.Task[object]] = field(default=None, repr=False)
    halt_step: int = 0
    halt_required: int = 4
    halt_expected: str = "TURN AROUND"
    snared_until: float = 0.0
    door100_phase: int = 0
    switches_found: int = 0
    door100_code: str = ""

    @property
    def busy(self) -> bool:
        return any((self.threat, self.dupe, self.seek, self.heartbeat, self.hiding, self.eyes_active, self.screech_active, self.halt_active, self.door100_phase > 0))


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
    def __init__(self, bot: "DoorsBot", session: GameSession, chase_type: int = 1) -> None:
        super().__init__(bot, session)
        self.chase_type = chase_type
        self.add_item(self.DirectionButton("LEFT", "⬅️"))
        self.add_item(self.DirectionButton("RIGHT", "➡️"))
        self.add_item(self.DirectionButton("CROUCH", "🫥"))
        if chase_type == 2:
            self.add_item(self.DirectionButton("AVOID", "🔥"))

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
                self.bot.seek_timeout(session, challenge.index, chase_type=self.chase_type)
            )
            remaining = " → ".join(challenge.sequence[challenge.index :])
            wait_time = QTE_SECONDS if self.chase_type == 1 else 3.0
            await interaction.response.edit_message(
                content=(
                    "👁️ SEEK CHASE\n"
                    f"Correct. Next movement: **{challenge.sequence[challenge.index]}**\n"
                    f"Sequence remaining: `{remaining}`\n"
                    f"You have {wait_time:.0f} seconds."
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


class ClosetView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session)
        self.add_item(self.ExitButton())

    class ExitButton(discord.ui.Button["ClosetView"]):
        def __init__(self) -> None:
            super().__init__(
                label="EXIT CLOSET",
                style=discord.ButtonStyle.primary,
                custom_id="doors_exit_closet",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if not session.hiding:
                    await interaction.response.send_message("You are not in a closet.", ephemeral=True)
                    return
                session.hiding = False
                cancel_task(session.hide_task)
                session.hide_task = None
                self.stop()
                await interaction.response.edit_message(
                    content="You exit the closet.",
                    view=None,
                )

class JeffShopView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session)
        self.add_item(self.BuyButton("Crucifix", 500, "crucifix", "✝️"))
        self.add_item(self.BuyButton("Skeleton Key", 300, "skeleton_key", "🗝️"))
        self.add_item(self.BuyButton("Vitamins", 100, "vitamins", "💊"))
        self.add_item(self.BuyButton("Flashlight", 150, "flashlight", "🔦"))

    class BuyButton(discord.ui.Button["JeffShopView"]):
        def __init__(self, name: str, cost: int, item_id: str, emoji: str) -> None:
            super().__init__(
                label=f"{name} ({cost}g)",
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"jeff_shop_{item_id}",
            )
            self.item_name = name
            self.cost = cost
            self.item_id = item_id

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if session.wallet < self.cost:
                    await interaction.response.send_message("Not enough gold.", ephemeral=True)
                    return
                if self.item_id in session.inventory:
                    await interaction.response.send_message("You already have this item.", ephemeral=True)
                    return

                session.wallet -= self.cost
                session.inventory.add(self.item_id)
                self.disabled = True
                await interaction.response.edit_message(view=self.view)
                await interaction.followup.send(f"🪙 Bought **{self.item_name}**. Remaining gold: {session.wallet}", ephemeral=False)

class EyesView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session)
        self.add_item(self.LookAwayButton())

    class LookAwayButton(discord.ui.Button["EyesView"]):
        def __init__(self) -> None:
            super().__init__(
                label="LOOK AWAY",
                style=discord.ButtonStyle.danger,
                custom_id="doors_look_away",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if not session.eyes_active:
                    await interaction.response.send_message("Eyes is not here.", ephemeral=True)
                    return
                session.eyes_active = False
                cancel_task(session.eyes_task)
                session.eyes_task = None
                self.stop()
                await interaction.response.edit_message(
                    content="✅ You look away quickly. Eyes disappears.",
                    view=None,
                )

class ScreechView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session)
        self.add_item(self.LookBehindButton())

    class LookBehindButton(discord.ui.Button["ScreechView"]):
        def __init__(self) -> None:
            super().__init__(
                label="LOOK BEHIND",
                style=discord.ButtonStyle.primary,
                custom_id="doors_look_behind",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if not session.screech_active:
                    await interaction.response.send_message("Screech is not here.", ephemeral=True)
                    return
                session.screech_active = False
                cancel_task(session.screech_task)
                session.screech_task = None
                self.stop()
                await interaction.response.edit_message(
                    content="✅ You look right at Screech! It screams and retreats.",
                    view=None,
                )

class HaltView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession, expected: str) -> None:
        super().__init__(bot, session)
        self.add_item(self.HaltAction(expected))

    class HaltAction(discord.ui.Button["HaltView"]):
        def __init__(self, expected: str) -> None:
            super().__init__(
                label=expected,
                style=discord.ButtonStyle.danger,
                custom_id=f"doors_halt_{expected.replace(' ', '_').lower()}",
            )
            self.expected = expected

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if not session.halt_active:
                    await interaction.response.send_message("Halt is over.", ephemeral=True)
                    return

                if session.halt_expected != self.expected:
                    await interaction.response.defer()
                    await self.view.bot.kill_session(session, "💀 You moved wrong! Halt catches you.", "Halt")
                    return

                session.halt_step += 1
                cancel_task(session.halt_task)

                if session.halt_step >= session.halt_required:
                    session.halt_active = False
                    session.halt_task = None
                    self.stop()
                    await interaction.response.edit_message(
                        content="✅ You survived Halt's hallway and escaped.",
                        view=None,
                    )
                    return

                # Next phase
                next_expected = "RUN" if self.expected == "TURN AROUND" else "TURN AROUND"
                session.halt_expected = next_expected
                session.halt_task = asyncio.create_task(self.view.bot.halt_timeout(session, session.halt_step))

                await interaction.response.edit_message(
                    content=f"🔵 **HALT CHASE**\nQuick, click **[{next_expected}]**!",
                    view=HaltView(self.view.bot, session, next_expected)
                )

class FigureEncounterView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session)
        self.add_item(self.PullLeverButton())

    class PullLeverButton(discord.ui.Button["FigureEncounterView"]):
        def __init__(self) -> None:
            super().__init__(
                label="PULL LEVER",
                style=discord.ButtonStyle.danger,
                custom_id="doors_pull_lever",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if session.door100_phase != 1:
                    await interaction.response.send_message("You can't do this now.", ephemeral=True)
                    return
                session.door100_phase = 2
                session.hiding = True
                session.hide_task = asyncio.create_task(self.view.bot.hide_timeout(session))
                self.stop()
                await interaction.response.edit_message(
                    content="⚡ The lights come on, but Figure drops down! Quick, you dive into a closet!",
                    view=ClosetView(self.view.bot, session)
                )

class BreakerPuzzleView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session)
        self.switches = [False] * 10
        for i in range(10):
            self.add_item(self.ToggleButton(i))
        self.add_item(self.SubmitButton())

    class ToggleButton(discord.ui.Button["BreakerPuzzleView"]):
        def __init__(self, index: int) -> None:
            super().__init__(
                label=f"SW {index+1}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"breaker_sw_{index}",
                row=index // 5
            )
            self.index = index

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            self.view.switches[self.index] = not self.view.switches[self.index]
            self.style = discord.ButtonStyle.success if self.view.switches[self.index] else discord.ButtonStyle.secondary
            await interaction.response.edit_message(view=self.view)

    class SubmitButton(discord.ui.Button["BreakerPuzzleView"]):
        def __init__(self) -> None:
            super().__init__(
                label="SUBMIT CODE",
                style=discord.ButtonStyle.primary,
                custom_id="breaker_submit",
                row=2
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session

            # Map switches to binary string
            current_code = "".join("1" if s else "0" for s in self.view.switches)
            if current_code == session.door100_code:
                session.door100_phase = 4
                self.view.stop()
                await interaction.response.edit_message(
                    content="✅ The elevator powers on! The door opens. Run for it!",
                    view=ElevatorEscapeView(self.view.bot, session)
                )
            else:
                session.health -= 25
                if session.health <= 0:
                    await interaction.response.defer()
                    await self.view.bot.kill_session(session, "💀 The sparks alert Figure! It finds you.", "Figure")
                else:
                    await interaction.response.send_message(
                        f"❌ BZZT! Wrong code. You took 25 damage. Health: {session.health}/100",
                        ephemeral=True
                    )

class ElevatorEscapeView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session)
        self.add_item(self.EscapeButton())
        self.timeout_task = asyncio.create_task(self.start_timer())

    async def start_timer(self):
        try:
            await asyncio.sleep(5.0)
            async with self.session.lock:
                if self.session.door100_phase == 4 and not self.session.escaped:
                    await self.bot.kill_session(self.session, "💀 You didn't make it to the elevator in time! Figure caught you.", "Figure")
        except asyncio.CancelledError:
            pass

    class EscapeButton(discord.ui.Button["ElevatorEscapeView"]):
        def __init__(self) -> None:
            super().__init__(
                label="RUN TO ELEVATOR",
                style=discord.ButtonStyle.success,
                custom_id="doors_elevator_escape",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if session.door100_phase != 4:
                    return
                session.escaped = True
                cancel_task(self.view.timeout_task)
                self.view.stop()

                embed = discord.Embed(
                    title="🎉 You Escaped Floor 1!",
                    description="You dive into the elevator as the doors close on Figure.\n\n"
                                f"🪙 Coins: **{session.wallet}**\n"
                                f"❤️ Health: **{session.health}/100**",
                    color=discord.Color.gold()
                )
                await interaction.response.edit_message(content="", embed=embed, view=None)

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
        commands_to_add = (
            self.start_command,
            self.next_command,
            self.look_around_command,
            self.loot_command,
            self.closet_command,
            self.door_command,
            self.search_book_command,
            self.crack_code_command,
            self.search_switches_command,
            self.status_command,
            self.crouch_command,
            self.left_command,
            self.right_command,
            self.doors_help_command,
            self.reset_command,
            self.talk_command,
        )
        for command in commands_to_add:
            # Commands declared on a Bot subclass are class descriptors. Set
            # their binding explicitly so callbacks receive the live bot
            # instance instead of being invoked as unbound functions.
            command.binding = self
            self.tree.add_command(command)

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

    async def eyes_timeout(self, session: GameSession) -> None:
        try:
            await asyncio.sleep(2.0)
            while True:
                async with session.lock:
                    if not session.eyes_active:
                        break
                    session.health -= 10
                    if session.health <= 0:
                        await self.kill_session(session, "💀 You stared at Eyes for too long.", "Eyes")
                        break
                    if session.last_channel:
                        await session.last_channel.send(f"👁️ Eyes drains 10 HP! Health: **{session.health}/100**")
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return

    async def screech_timeout(self, session: GameSession) -> None:
        try:
            delay = random.uniform(2.0, 4.0)
            await asyncio.sleep(delay)
            async with session.lock:
                if not session.screech_active:
                    return
                if session.last_channel:
                    await session.last_channel.send(
                        "**Psst!**\nQuick, use the button!",
                        view=ScreechView(self, session)
                    )

            qte_time = 2.0 + (2.0 if "vitamins" in session.inventory else 0.0)
            await asyncio.sleep(qte_time)

            async with session.lock:
                if session.screech_active:
                    if "crucifix" in session.inventory:
                        session.inventory.remove("crucifix")
                        session.screech_active = False
                        if session.last_channel:
                            await session.last_channel.send("✝️ The Crucifix activates! **Screech** is banished to the abyss.")
                    else:
                        session.health -= 40
                        session.screech_active = False
                        if session.health <= 0:
                            await self.kill_session(session, "💀 Screech bit you in the dark.", "Screech")
                        elif session.last_channel:
                            await session.last_channel.send(f"💀 **Screech bit you!** You take 40 damage. Health: **{session.health}/100**")
        except asyncio.CancelledError:
            return

    async def halt_timeout(self, session: GameSession, expected_step: int) -> None:
        try:
            qte_time = 2.0 + (2.0 if "vitamins" in session.inventory else 0.0)
            await asyncio.sleep(qte_time)
            async with session.lock:
                if session.halt_active and session.halt_step == expected_step:
                    await self.kill_session(session, "💀 You hesitated in Halt's hallway.", "Halt")
        except asyncio.CancelledError:
            return

    async def roll_room_event(self, session: GameSession) -> None:
        channel = session.last_channel
        if channel is None:
            return
        roll = random.randint(1, 100)

        # Halt (Doors 55-65)
        if 55 <= session.current_door <= 65 and roll <= 15:
            session.halt_active = True
            session.halt_step = 0
            session.halt_expected = "TURN AROUND"
            session.halt_task = asyncio.create_task(self.halt_timeout(session, 0))
            await channel.send(
                "🔵 **HALT CHASE**\nThe hallway stretches forever. A blue screen flashes...",
                view=HaltView(self, session, "TURN AROUND")
            )
            return

        # Eyes (Doors 53+)
        if session.current_door >= 53 and roll <= 25 and roll > 15:
            session.eyes_active = True
            session.eyes_task = asyncio.create_task(self.eyes_timeout(session))
            await channel.send(
                "👁️ **LOOK AWAY!**\nA purple glow fills the room...",
                view=EyesView(self, session)
            )
            return

        # Dark Room / Screech
        # Doors 90-98 (Greenhouse) are always dark. Otherwise 20% chance.
        is_dark = (90 <= session.current_door <= 98) or (roll <= 35 and roll > 25)

        if is_dark:
            await channel.send("🌑 **The room is pitch black.**")
            if "flashlight" not in session.inventory:
                if random.randint(1, 100) <= 50:
                    session.screech_active = True
                    session.screech_task = asyncio.create_task(self.screech_timeout(session))
                    return

        # Greenhouse logic
        if 90 <= session.current_door <= 98:
            if random.random() < 0.20:
                session.snared_until = clock() + 3.0
                await channel.send("🌿 **SNAP!** You stepped in a snare! You can't move for 3 seconds.")

            if roll <= 25:
                await self.start_threat(session, "rush", silent=True)
                return

        # Normal threats
        if roll <= 10:
            await self.start_threat(session, "ambush")
        elif roll <= 20:
            await self.start_threat(session, "rush")
        else:
            if not is_dark:
                await channel.send("The room is quiet. For now.")

    async def start_threat(self, session: GameSession, kind: str, silent: bool = False) -> None:
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
            if silent:
                prompt = (
                    "🔊 *(You hear a distant roaring sound getting closer...)*\n"
                    "🚪 Available Closets: 4\n"
                    "Quick! Hide using `/closet`!"
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
                    if "crucifix" in session.inventory:
                        session.inventory.remove("crucifix")
                        entity = session.threat.kind.capitalize()
                        session.threat = None
                        if session.last_channel:
                            await session.last_channel.send(f"✝️ The Crucifix activates! **{entity}** is banished to the ground. You are safe... for now.")
                    else:
                        await self.kill_session(
                            session,
                            "💀 You were too slow. The entity found you.",
                            session.threat.kind.capitalize()
                        )
        except asyncio.CancelledError:
            return

    async def seek_timeout(self, session: GameSession, expected_index: int, chase_type: int = 1) -> None:
        try:
            base_delay = QTE_SECONDS if chase_type == 1 else 3.0
            delay = base_delay + 2.0 if "vitamins" in session.inventory else base_delay
            await asyncio.sleep(delay)
            async with session.lock:
                if session.seek and session.seek.index == expected_index:
                    if "crucifix" in session.inventory:
                        session.inventory.remove("crucifix")
                        session.seek = None
                        if session.last_channel:
                            await session.last_channel.send("✝️ The Crucifix activates! **Seek** is banished. The chase ends.")
                    else:
                        await self.kill_session(
                            session,
                            "💀 You hesitate for too long. Seek catches you.",
                            "Seek"
                        )
        except asyncio.CancelledError:
            return

    async def heartbeat_timeout(
        self, session: GameSession, expected_index: int
    ) -> None:
        try:
            delay = QTE_SECONDS + 2.0 if "vitamins" in session.inventory else QTE_SECONDS
            await asyncio.sleep(delay)
            async with session.lock:
                if session.heartbeat and session.heartbeat.index == expected_index:
                    if "crucifix" in session.inventory:
                        session.inventory.remove("crucifix")
                        session.heartbeat = None
                        if session.last_channel:
                            await session.last_channel.send("✝️ The Crucifix activates! **Figure** recoils, giving you time to escape.")
                    else:
                        await self.kill_session(
                            session,
                            "💀 You miss the heartbeat window. Figure hears you.",
                            "Figure"
                        )
        except asyncio.CancelledError:
            return

    async def kill_session(self, session: GameSession, reason: str, entity_hint: str = "") -> None:
        session.dead = True
        cancel_task(session.threat.task if session.threat else None)
        cancel_task(session.seek.task if session.seek else None)
        cancel_task(session.heartbeat.task if session.heartbeat else None)
        cancel_task(session.look_task)
        cancel_task(session.hide_task)
        session.threat = None
        session.seek = None
        session.heartbeat = None
        session.look_task = None
        session.hide_task = None
        channel = session.last_channel
        if channel:
            embed = discord.Embed(
                title="Guiding Light",
                description=f"{reason}\n\n**Run over at Door {session.current_door}.** Use `/start` to try again.",
                color=discord.Color.blue()
            )
            if entity_hint:
                if entity_hint == "Rush":
                    embed.add_field(name="Hint", value="When the lights flicker, hide in a closet immediately.")
                elif entity_hint == "Ambush":
                    embed.add_field(name="Hint", value="Ambush rebounds! You must repeatedly exit and re-enter the closet.")
                elif entity_hint == "Seek":
                    embed.add_field(name="Hint", value="Follow the guiding light quickly during the chase.")
                elif entity_hint == "Figure":
                    embed.add_field(name="Hint", value="Figure is blind but hears everything. Crouch and match the heartbeat.")
                elif entity_hint == "Timothy":
                    embed.add_field(name="Hint", value="Timothy sometimes hides in drawers. Keep your health up.")
                elif entity_hint == "Dupe":
                    embed.add_field(name="Hint", value="Remember the number of the door you just came from!")
                elif entity_hint == "Hide":
                    embed.add_field(name="Hint", value="You cannot hide forever. Get out of the closet before Hide finds you.")
            await channel.send(embed=embed)

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

    @app_commands.command(name="next", description="Advance 1–2 doors.")
    @app_commands.describe(amount="How many doors to advance (1–2).")
    async def next_command(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 2] = 1
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

            if now < session.snared_until:
                await interaction.response.send_message(
                    f"🌿 You are snared! Wait {session.snared_until - now:.1f}s.",
                    ephemeral=True,
                )
                return

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
            if target >= 100:
                session.current_door = 100
                session.door100_phase = 1
                session.door100_code = "".join(random.choice(["0", "1"]) for _ in range(10))
                await interaction.response.send_message(
                    "🚪 **Door 100: The Electrical Breaker Room**\n"
                    "You step into a massive warehouse. The elevator is powered down.\n"
                    "At the end of the hall is the breaker switch. Pull the lever to start the sequence.",
                    view=FigureEncounterView(self, session)
                )
                return

            if amount == 2 and session.current_door >= 10 and random.random() < 0.35:
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

            if session.current_door == 52:
                await interaction.response.send_message(
                    "🛒 **Door 52: Jeff's Shop**\n"
                    "A rare moment of peace. Jeff waves at you from behind his counter. "
                    "El Goblino is sitting nearby. Use buttons to buy items, or `/talk` to hear a tip.\n"
                    f"Your wallet: **{session.wallet}g**",
                    view=JeffShopView(self, session)
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

        chase_type = 2 if session.current_door >= 70 else 1
        options = ["LEFT", "RIGHT", "CROUCH"]
        if chase_type == 2:
            options.append("AVOID")

        sequence = [random.choice(options) for _ in range(5 if chase_type == 1 else 7)]
        challenge = SeekChallenge(sequence=sequence)
        session.seek = challenge
        challenge.task = asyncio.create_task(self.seek_timeout(session, 0, chase_type=chase_type))

        wait_time = QTE_SECONDS if chase_type == 1 else 3.0
        msg = (
            "👁️ **SEEK CHASE INITIATED**\n"
            "You are running down a long corridor. Seek is right behind you!\n"
            f"🔵 Guiding Light: **{sequence[0]}**\n"
            f"Use the buttons below in the next {wait_time:.0f} seconds."
        )
        if chase_type == 2:
            msg = "🔥 **SEEK CHASE #2**\nThe hallway is crumbling and burning!\n" + msg

        challenge.message = await channel.send(
            msg,
            view=SeekView(self, session, chase_type=chase_type),
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

    @app_commands.command(name="loot", description="Collect coins in the current room.")
    async def loot_command(self, interaction: discord.Interaction) -> None:
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

            timothy_msg = ""
            if random.random() < 0.05:
                session.health -= 5
                timothy_msg = f"\n🕷️ **Timothy jumped out!** You take 5 damage. Health: **{session.health}/100**."
                if session.health <= 0:
                    await interaction.response.defer()
                    await self.kill_session(session, "🕷️ Timothy delivered the final blow.", "Timothy")
                    return

            await interaction.response.send_message(
                f"🪙 You collect **{amount} coins**. Wallet: **{session.wallet}**.{timothy_msg}"
            )

    async def hide_timeout(self, session: GameSession) -> None:
        try:
            await asyncio.sleep(8.0)
            async with session.lock:
                if session.hiding:
                    session.hiding = False
                    session.health -= 40
                    session.hide_task = None
                    channel = session.last_channel
                    if session.health <= 0:
                        await self.kill_session(session, "💀 Hide forcefully kicks you out of the closet.", "Hide")
                    elif channel:
                        await channel.send("💀 **Hide forcefully kicks you out!** You take 40 damage.")
        except asyncio.CancelledError:
            return

    @app_commands.command(name="closet", description="Hide in a closet.")
    async def closet_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if session.hiding:
                await interaction.response.send_message(
                    "You are already in a closet.", ephemeral=True
                )
                return

            session.hiding = True
            session.hide_task = asyncio.create_task(self.hide_timeout(session))

            if session.threat:
                threat = session.threat
                threat.closet_uses += 1
                if threat.kind == "rush":
                    cancel_task(threat.task)
                    session.threat = None
                    await interaction.response.send_message(
                        "🚪 You slam the closet shut. Rush tears past, then vanishes.",
                        view=ClosetView(self, session)
                    )
                    return
                if threat.closet_uses >= threat.required_closet_uses:
                    cancel_task(threat.task)
                    session.threat = None
                    await interaction.response.send_message(
                        "🚪 You exit and re-enter the closet at exactly the right moment. "
                        "Ambush finally gives up.",
                        view=ClosetView(self, session)
                    )
                    return
                await interaction.response.send_message(
                    f"🚪 Ambush rebounds! Quickly exit and re-enter the closet! "
                    f"Progress: **{threat.closet_uses}/{threat.required_closet_uses}**",
                    view=ClosetView(self, session)
                )
            else:
                await interaction.response.send_message(
                    "🚪 You hide in the closet. Don't stay too long...",
                    view=ClosetView(self, session)
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
        name="search_switches", description="Search for breaker switches in Door 100."
    )
    async def search_switches_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if session.door100_phase < 2:
                await interaction.response.send_message("There are no switches to find here.", ephemeral=True)
                return
            if session.hiding:
                await interaction.response.send_message("You are hiding! Exit the closet first.", ephemeral=True)
                return
            if session.door100_phase >= 3:
                await interaction.response.send_message("You already found all the switches.", ephemeral=True)
                return

            await interaction.response.defer()
            await asyncio.sleep(random.uniform(1.0, 2.0))

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
                    f"First color: **{sequence[0]}**",
                    view=HeartbeatView(self, session),
                )
            else:
                session.switches_found += 1
                if session.switches_found >= 10:
                    session.door100_phase = 3
                    code_display = "".join("ON " if c == "1" else "OFF " for c in session.door100_code).strip()
                    await interaction.followup.send(
                        f"⚡ You found the final switch! (10/10)\n"
                        f"**Target Pattern:** {code_display}\n"
                        "Use the switches below to match the pattern, then press SUBMIT CODE.",
                        view=BreakerPuzzleView(self, session)
                    )
                else:
                    await interaction.followup.send(
                        f"⚡ You found a breaker switch! ({session.switches_found}/10)"
                    )

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

    @app_commands.command(name="talk", description="Talk to El Goblino at Jeff's Shop.")
    async def talk_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        async with session.lock:
            if session.current_door != 52:
                await interaction.response.send_message("El Goblino is only at Door 52.", ephemeral=True)
                return

            tips = [
                "Hey man, watch out for the dark rooms... something might whisper to you.",
                "I heard Figure is blind... but his hearing is something else. Better crouch!",
                "Jeff says hi. He likes gold. You got gold?",
                "Crucifix? Yeah, it'll save your life once. Best investment you'll make."
            ]
            await interaction.response.send_message(f"👹 **El Goblino:** {random.choice(tips)}")

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