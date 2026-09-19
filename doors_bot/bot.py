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
SAFE_HIDE_SECONDS = 8.0
LOCKED_DOORS = {14, 22, 37, 60, 73}
SEEK_DOORS = {30, 70}
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
    badges: set[str] = field(default_factory=set)
    entities_survived: set[str] = field(default_factory=set)
    crucifix_used: bool = False
    infirmary_unlocked: bool = False
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
    door100_switches: list[bool] = field(default_factory=lambda: [False] * 10)
    room_messages: list[discord.Message] = field(default_factory=list, repr=False)

    @property
    def busy(self) -> bool:
        return any(
            (
                self.threat,
                self.dupe,
                self.seek,
                self.heartbeat,
                self.hiding,
                self.eyes_active,
                self.screech_active,
                self.halt_active,
                self.door100_phase in (1, 3, 4),
            )
        )


class GameButtonView(discord.ui.View):
    """Base view that keeps button interactions tied to one player."""

    def __init__(self, bot: "DoorsBot", session: GameSession, timeout: Optional[float] = 180.0) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.session = session

    async def reject_other_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message(
                "❌ This is not your game session.", ephemeral=True
            )
            return True
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True


class SeekView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession, chase_type: int = 1) -> None:
        super().__init__(bot, session, timeout=60.0)
        self.chase_type = chase_type
        self.add_item(self.DirectionButton("LEFT", "⬅️"))
        self.add_item(self.DirectionButton("RIGHT", "➡️"))
        if chase_type == 2:
            self.add_item(self.DirectionButton("DUCK", "🦆"))
            self.add_item(self.DirectionButton("AVOID", "✋"))
        else:
            self.add_item(self.DirectionButton("CROUCH", "🫥"))

    class DirectionButton(discord.ui.Button["SeekView"]):
        def __init__(self, action: str, emoji: str) -> None:
            label_map = {
                "LEFT": "LEFT",
                "RIGHT": "RIGHT",
                "CROUCH": "CROUCH",
                "DUCK": "DUCK UNDER BEAM",
                "AVOID": "AVOID HANDS",
            }
            super().__init__(
                label=label_map.get(action, action),
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
                if "crucifix" in session.inventory:
                    session.inventory.remove("crucifix")
                    session.crucifix_used = True
                    session.badges.add("Evil Be Gone")
                    session.entities_survived.add("Seek")
                    cancel_task(challenge.task)
                    session.seek = None
                    self.stop()
                    await interaction.response.edit_message(
                        content="✝️ The Crucifix activates! Holy chains ensnare **Seek**, dragging it into a glowing vortex! The chase is broken.",
                        view=None,
                    )
                    return

                await interaction.response.defer()
                await self.bot.kill_session(
                    session,
                    f"❌ You chose {action.lower()} instead of {expected.lower()}. Seek catches you.",
                    "Seek",
                )
                return

            challenge.index += 1
            cancel_task(challenge.task)
            if challenge.index >= len(challenge.sequence):
                session.seek = None
                session.entities_survived.add("Seek")
                self.stop()
                await interaction.response.edit_message(
                    content=(
                        "✅ You dive through the reinforced fire door and slam it shut behind you!\n"
                        "Seek crashes violently against the iron and dissolves into bubbling black sludge."
                    ),
                    view=None,
                )
                return

            challenge.task = asyncio.create_task(
                self.bot.seek_timeout(session, challenge.index, chase_type=self.chase_type)
            )
            wait_time = (3.0 if self.chase_type == 2 else QTE_SECONDS) + (2.0 if "vitamins" in session.inventory else 0.0)
            next_prompt = challenge.sequence[challenge.index]
            display_map = {
                "LEFT": "⬅️ LEFT",
                "RIGHT": "➡️ RIGHT",
                "CROUCH": "🫥 CROUCH",
                "DUCK": "🦆 DUCK UNDER FALLEN BEAM",
                "AVOID": "✋ AVOID REACHING HANDS",
            }
            await interaction.response.edit_message(
                content=(
                    f"🔥 **SEEK CHASE ({challenge.index + 1}/{len(challenge.sequence)})**\n"
                    f"Guiding Light illuminates the way: **{display_map.get(next_prompt, next_prompt)}**\n"
                    f"React within **{wait_time:.1f} seconds**!"
                ),
                view=self,
            )


class HeartbeatView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session, timeout=45.0)
        self.add_item(self.ColorButton("RED", "🔴", discord.ButtonStyle.danger))
        self.add_item(self.ColorButton("BLUE", "🔵", discord.ButtonStyle.primary))
        self.add_item(self.ColorButton("GREEN", "🟢", discord.ButtonStyle.success))

    class ColorButton(discord.ui.Button["HeartbeatView"]):
        def __init__(
            self, color: str, emoji: str, style: discord.ButtonStyle
        ) -> None:
            super().__init__(
                label=color,
                emoji=emoji,
                style=style,
                custom_id=f"doors_heartbeat_{color.lower()}",
            )
            self.color = color

        async def callback(self, interaction: discord.Interaction) -> None:
            await self.view.handle_action(interaction, self.color)

    async def handle_action(
        self, interaction: discord.Interaction, color: str
    ) -> None:
        if await self.reject_other_user(interaction):
            return
        session = self.session
        async with session.lock:
            challenge = session.heartbeat
            if not challenge:
                await interaction.response.send_message(
                    "Figure has already walked away.", ephemeral=True
                )
                return

            expected = challenge.sequence[challenge.index]
            if color != expected:
                await interaction.response.defer()
                await self.bot.kill_session(
                    session,
                    f"❌ Missed heartbeat beat. You tapped {color}, but needed {expected}. Figure pinpoints your breath.",
                    "Figure",
                )
                return

            challenge.index += 1
            cancel_task(challenge.task)
            if challenge.index >= len(challenge.sequence):
                session.heartbeat = None
                session.entities_survived.add("Figure")
                self.stop()
                await interaction.response.edit_message(
                    content=(
                        "💓 You steady your breath and maintain absolute silence.\n"
                        "Figure growls and stalks deeper into the stacks."
                    ),
                    view=None,
                )
                return

            challenge.task = asyncio.create_task(
                self.bot.heartbeat_timeout(session, challenge.index)
            )
            next_color = challenge.sequence[challenge.index]
            qte_time = QTE_SECONDS + (2.0 if "vitamins" in session.inventory else 0.0)
            await interaction.response.edit_message(
                content=(
                    f"💓 Beat {challenge.index + 1}/{len(challenge.sequence)}: "
                    f"Next color: **{next_color}** ({qte_time:.1f}s)"
                ),
                view=self,
            )


class ClosetView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session, timeout=12.0)
        self.add_item(self.ExitButton())

    class ExitButton(discord.ui.Button["ClosetView"]):
        def __init__(self) -> None:
            super().__init__(
                label="EXIT CLOSET",
                emoji="🚪",
                style=discord.ButtonStyle.secondary,
                custom_id="doors_exit_closet",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if not session.hiding:
                    await interaction.response.send_message("You are not inside a closet.", ephemeral=True)
                    return
                session.hiding = False
                cancel_task(session.hide_task)
                session.hide_task = None
                self.view.stop()
                await interaction.response.edit_message(
                    content="🚪 You push open the closet doors and step back into the corridor. The air is quiet.",
                    view=None,
                )


class JeffShopView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session, timeout=300.0)
        self.add_item(self.BuyButton("Crucifix", 500, "crucifix", "✝️", "Auto-banish 1 entity"))
        self.add_item(self.BuyButton("Skeleton Key", 300, "skeleton_key", "🗝️", "Unlocks Infirmary"))
        self.add_item(self.BuyButton("Vitamins", 100, "vitamins", "💊", "+2s QTE window"))
        self.add_item(self.BuyButton("Flashlight", 150, "flashlight", "🔦", "Ward off Screech"))

    class BuyButton(discord.ui.Button["JeffShopView"]):
        def __init__(self, name: str, cost: int, item_id: str, emoji: str, desc: str) -> None:
            super().__init__(
                label=f"{name} ({cost}g)",
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"jeff_shop_{item_id}",
            )
            self.item_name = name
            self.cost = cost
            self.item_id = item_id
            self.desc = desc

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if session.wallet < self.cost:
                    await interaction.response.send_message(
                        f"❌ Not enough gold. You have **{session.wallet}g**, but {self.item_name} costs **{self.cost}g**.",
                        ephemeral=True
                    )
                    return
                if self.item_id in session.inventory:
                    await interaction.response.send_message(
                        f"You already have a **{self.item_name}** in your inventory.",
                        ephemeral=True
                    )
                    return
                session.wallet -= self.cost
                session.inventory.add(self.item_id)
                self.disabled = True
                await interaction.response.edit_message(view=self.view)
                msg = await interaction.followup.send(
                    f"🪙 Jeff nods happily and slides you the **{self.item_name}** ({self.desc})! Remaining gold: **{session.wallet}g**",
                    ephemeral=False
                )
                if msg:
                    session.room_messages.append(msg)


class InfirmaryView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session, timeout=120.0)
        self.add_item(self.UnlockButton())

    class UnlockButton(discord.ui.Button["InfirmaryView"]):
        def __init__(self) -> None:
            super().__init__(
                label="UNLOCK INFIRMARY (Skeleton Key)",
                emoji="🗝️",
                style=discord.ButtonStyle.success,
                custom_id="doors_unlock_infirmary",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if "skeleton_key" not in session.inventory:
                    await interaction.response.send_message("❌ You do not have a Skeleton Key.", ephemeral=True)
                    return
                session.inventory.remove("skeleton_key")
                session.infirmary_unlocked = True
                session.health = 100
                session.badges.add("Master of Keys")
                if "vitamins" not in session.inventory:
                    session.inventory.add("vitamins")
                self.view.stop()
                await interaction.response.edit_message(
                    content=(
                        "🏥 **INFIRMARY UNLOCKED!**\n"
                        "You insert the ornate Skeleton Key into the skull lock. The heavy iron bars swing open.\n"
                        "Inside, you find sterile bandages and medicine: **Health fully restored to 100 HP**!\n"
                        "You also scavenged a bottle of **Vitamins** (+2s QTE time)."
                    ),
                    view=None,
                )


class EyesView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session, timeout=30.0)
        self.add_item(self.LookAwayButton())

    class LookAwayButton(discord.ui.Button["EyesView"]):
        def __init__(self) -> None:
            super().__init__(
                label="LOOK AWAY",
                emoji="🙈",
                style=discord.ButtonStyle.danger,
                custom_id="doors_look_away",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if not session.eyes_active:
                    await interaction.response.send_message("Eyes has already departed.", ephemeral=True)
                    return
                session.eyes_active = False
                cancel_task(session.eyes_task)
                session.eyes_task = None
                session.entities_survived.add("Eyes")
                self.view.stop()
                await interaction.response.edit_message(
                    content="✅ You immediately avert your gaze and face the floor. The purple hum fades away.",
                    view=None,
                )


class ScreechView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session, timeout=10.0)
        self.add_item(self.LookBehindButton())

    class LookBehindButton(discord.ui.Button["ScreechView"]):
        def __init__(self) -> None:
            super().__init__(
                label="LOOK BEHIND",
                emoji="👀",
                style=discord.ButtonStyle.primary,
                custom_id="doors_look_behind",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if not session.screech_active:
                    await interaction.response.send_message("Screech is no longer there.", ephemeral=True)
                    return
                session.screech_active = False
                cancel_task(session.screech_task)
                session.screech_task = None
                session.entities_survived.add("Screech")
                self.view.stop()
                await interaction.response.edit_message(
                    content="✅ You whipped around and stared down Screech! It lets out a high-pitched shriek and dissolves into black dust.",
                    view=None,
                )


class HaltView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession, expected: str) -> None:
        super().__init__(bot, session, timeout=15.0)
        self.expected = expected
        self.add_item(self.HaltAction(expected))

    class HaltAction(discord.ui.Button["HaltView"]):
        def __init__(self, expected: str) -> None:
            emoji = "🔄" if expected == "TURN AROUND" else "🏃"
            super().__init__(
                label=expected,
                emoji=emoji,
                style=discord.ButtonStyle.danger if expected == "TURN AROUND" else discord.ButtonStyle.primary,
                custom_id=f"doors_halt_{expected.replace(' ', '_').lower()}",
            )
            self.expected = expected

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if not session.halt_active:
                    await interaction.response.send_message("Halt chase has ended.", ephemeral=True)
                    return

                if session.halt_expected != self.expected:
                    if "crucifix" in session.inventory:
                        session.inventory.remove("crucifix")
                        session.crucifix_used = True
                        session.badges.add("Evil Be Gone")
                        session.halt_active = False
                        cancel_task(session.halt_task)
                        session.halt_task = None
                        session.entities_survived.add("Halt")
                        self.view.stop()
                        await interaction.response.edit_message(
                            content="✝️ The Crucifix activates! **Halt** is banished to the ground in blue lightning!",
                            view=None,
                        )
                        return

                    await interaction.response.defer()
                    await self.view.bot.kill_session(session, "💀 You moved the wrong way! Halt catches you.", "Halt")
                    return

                session.halt_step += 1
                cancel_task(session.halt_task)
                if session.halt_step >= session.halt_required:
                    session.halt_active = False
                    session.halt_task = None
                    session.entities_survived.add("Halt")
                    self.view.stop()
                    await interaction.response.edit_message(
                        content="✅ You survived all 4 cycles of Halt's hallway! The cyan mist clears, returning you to the hotel corridor.",
                        view=None,
                    )
                    return

                next_expected = "RUN" if self.expected == "TURN AROUND" else "TURN AROUND"
                session.halt_expected = next_expected
                session.halt_task = asyncio.create_task(self.view.bot.halt_timeout(session, session.halt_step))
                new_view = HaltView(self.view.bot, session, next_expected)
                qte_time = 2.0 + (2.0 if "vitamins" in session.inventory else 0.0)
                await interaction.response.edit_message(
                    content=(
                        f"🔵 **HALT CHASE (Cycle {session.halt_step + 1}/{session.halt_required})**\n"
                        f"The hallway flickers! Quick, press **[{next_expected}]** within {qte_time:.1f}s!"
                    ),
                    view=new_view,
                )


class FigureEncounterView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session, timeout=30.0)
        self.add_item(self.PullLeverButton())

    class PullLeverButton(discord.ui.Button["FigureEncounterView"]):
        def __init__(self) -> None:
            super().__init__(
                label="PULL LEVER",
                emoji="⚡",
                style=discord.ButtonStyle.danger,
                custom_id="doors_pull_lever",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            async with session.lock:
                if session.door100_phase != 1:
                    await interaction.response.send_message("The lever cannot be pulled right now.", ephemeral=True)
                    return
                session.door100_phase = 2
                session.hiding = True
                session.hide_task = asyncio.create_task(self.view.bot.hide_timeout(session))
                self.view.stop()
                await interaction.response.edit_message(
                    content=(
                        "⚡ **CLANK!** You pull the heavy iron breaker lever!\n"
                        "Emergency lights flicker on with a harsh buzz. A terrifying roar echoes through the rafters—\n"
                        "**FIGURE DROPS DOWN!** It stomps toward you! You dive headfirst into a nearby closet!\n"
                        "*(Safe inside closet for up to 8s. Figure passes by soon...)*"
                    ),
                    view=ClosetView(self.view.bot, session),
                )


class BreakerPuzzleView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session, timeout=180.0)
        self.switches = list(session.door100_switches)
        for i in range(10):
            self.add_item(self.ToggleButton(i))
        self.add_item(self.SubmitButton())

    class ToggleButton(discord.ui.Button["BreakerPuzzleView"]):
        def __init__(self, index: int) -> None:
            is_on = False
            super().__init__(
                label=f"SW {index+1}: [OFF]",
                style=discord.ButtonStyle.secondary,
                custom_id=f"breaker_sw_{index}",
                row=index // 5,
            )
            self.index = index

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            self.view.switches[self.index] = not self.view.switches[self.index]
            self.view.session.door100_switches[self.index] = self.view.switches[self.index]
            is_on = self.view.switches[self.index]
            self.style = discord.ButtonStyle.success if is_on else discord.ButtonStyle.secondary
            self.label = f"SW {self.index+1}: [{'ON' if is_on else 'OFF'}]"
            await interaction.response.edit_message(view=self.view)

    class SubmitButton(discord.ui.Button["BreakerPuzzleView"]):
        def __init__(self) -> None:
            super().__init__(
                label="⚡ SUBMIT CODE",
                emoji="🔌",
                style=discord.ButtonStyle.primary,
                custom_id="breaker_submit",
                row=2,
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            if await self.view.reject_other_user(interaction):
                return
            session = self.view.session
            current_code = "".join("1" if s else "0" for s in self.view.switches)
            if current_code == session.door100_code:
                session.door100_phase = 4
                session.badges.add("Expert Electrician")
                self.view.stop()
                await interaction.response.edit_message(
                    content=(
                        "✅ **BREAKER CODE ACCEPTED!**\n"
                        "The circuit breakers snap into place with a thunderous clatter!\n"
                        "The elevator gate rattles open across the warehouse! Figure lets out a deafening roar and sprints toward you!\n"
                        "**RUN FOR THE ELEVATOR!**"
                    ),
                    view=ElevatorEscapeView(self.view.bot, session),
                )
            else:
                session.health -= 25
                if session.health <= 0:
                    await interaction.response.defer()
                    await self.view.bot.kill_session(
                        session,
                        "💀 The electric sparks alert Figure! It bounds across the room and crushes you.",
                        "Figure",
                    )
                else:
                    await interaction.response.send_message(
                        f"❌ **BZZT! Wrong switch combination!** The arc flashes and shocks you for 25 damage. Health: **{session.health}/100**",
                        ephemeral=True,
                    )


class ElevatorEscapeView(GameButtonView):
    def __init__(self, bot: "DoorsBot", session: GameSession) -> None:
        super().__init__(bot, session, timeout=6.0)
        self.add_item(self.EscapeButton())
        self.timeout_task = asyncio.create_task(self.start_timer())

    async def start_timer(self) -> None:
        try:
            await asyncio.sleep(5.0)
            async with self.session.lock:
                if self.session.door100_phase == 4 and not self.session.escaped:
                    await self.bot.kill_session(
                        self.session,
                        "💀 You didn't reach the elevator in time! Figure tackled you before the gate closed.",
                        "Figure",
                    )
        except asyncio.CancelledError:
            pass

    class EscapeButton(discord.ui.Button["ElevatorEscapeView"]):
        def __init__(self) -> None:
            super().__init__(
                label="RUN TO ELEVATOR",
                emoji="🏃",
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

                session.badges.add("Rock Bottom")
                if session.wallet >= 300:
                    session.badges.add("High Roller")
                if session.health == 100:
                    session.badges.add("Untouchable")

                badge_str = "\n".join([f"🏅 **{b}**" for b in sorted(session.badges)]) or "🏅 **Rock Bottom**"
                embed = discord.Embed(
                    title="🎉 VICTORY — ESCAPED THE HOTEL (FLOOR 1)",
                    description=(
                        "You slide under the closing steel elevator gates as Figure slams against the glass!\n"
                        "The elevator cables whine and ascend rapidly. You survived all 100 doors.\n\n"
                        f"🪙 **Gold Collected:** {session.wallet}\n"
                        f"❤️ **Surviving Health:** {session.health}/100\n"
                        f"🚪 **Doors Conquered:** 100/100\n"
                        f"🛡️ **Entities Survived:** {', '.join(sorted(session.entities_survived)) or 'All encounters'}\n\n"
                        f"**Badges Unlocked:**\n{badge_str}"
                    ),
                    color=discord.Color.gold(),
                )
                await interaction.response.edit_message(content="", embed=embed, view=None)


class DoorsBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        self.sessions: dict[tuple[int, int], GameSession] = {}
        self.heartbeat_emoji = {
            "RED": "🔴",
            "BLUE": "🔵",
            "GREEN": "🟢",
        }

    async def setup_hook(self) -> None:
        self.tree.add_command(self.start_command)
        self.tree.add_command(self.next_command)
        self.tree.add_command(self.hide_command)
        self.tree.add_command(self.closet_command)
        self.tree.add_command(self.loot_command)
        self.tree.add_command(self.talk_command)
        self.tree.add_command(self.look_around_command)
        self.tree.add_command(self.search_switches_command)
        self.tree.add_command(self.door_command)
        self.tree.add_command(self.search_book_command)
        self.tree.add_command(self.crack_code_command)
        self.tree.add_command(self.crouch_command)
        self.tree.add_command(self.left_command)
        self.tree.add_command(self.right_command)
        self.tree.add_command(self.status_command)
        self.tree.add_command(self.doors_help_command)
        self.tree.add_command(self.reset_command)
        await self.tree.sync()
        logger.info("Application commands synced.")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "N/A")

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.error("Command error on %s: %s", interaction.command, error, exc_info=True)
        message = "An error occurred while running the command."
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Command is on cooldown. Try again in {error.retry_after:.1f}s."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    async def clear_room_messages(self, session: GameSession) -> None:
        """Deletes messages from the previous room so players cannot cheat on Dupe or previous rooms."""
        if not session.room_messages:
            return
        msgs = list(session.room_messages)
        session.room_messages.clear()
        for msg in msgs:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    async def send_tracked(
        self, session: GameSession, content: Optional[str] = None, **kwargs
    ) -> Optional[discord.Message]:
        channel = session.last_channel
        if channel is None:
            return None
        try:
            msg = await channel.send(content=content, **kwargs)
            session.room_messages.append(msg)
            return msg
        except discord.HTTPException:
            return None

    async def require_session(self, interaction: discord.Interaction) -> Optional[GameSession]:
        key = (interaction.guild_id or 0, interaction.user.id)
        session = self.sessions.get(key)
        if session is None:
            await interaction.response.send_message(
                "You don't have an active DOORS run. Use `/start` to begin at Door 1.",
                ephemeral=True,
            )
            return None
        if session.dead:
            await interaction.response.send_message(
                "💀 Your run is over. Use `/start` to begin a new journey.",
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
        coins = random.randint(*COIN_RANGE)
        session.coins_amount = coins
        session.locked = door in LOCKED_DOORS
        session.key_found = False

        header = f"🚪 **Door {door}**"
        lines = [header]
        if session.locked:
            lines.append("🔒 **Status: Locked.** You need to find the room key. Use `/look_around`.")
        else:
            lines.append(f"🪙 A small stack of gold rests on a table. Type `/loot` to claim it.")

        await self.send_tracked(session, "\n".join(lines))

        if not session.locked and door >= 10:
            await self.roll_room_event(session)

    async def eyes_timeout(self, session: GameSession) -> None:
        try:
            reaction_time = 2.0 + (2.0 if "vitamins" in session.inventory else 0.0)
            await asyncio.sleep(reaction_time)
            while True:
                async with session.lock:
                    if not session.eyes_active:
                        break

                    if "crucifix" in session.inventory:
                        session.inventory.remove("crucifix")
                        session.crucifix_used = True
                        session.badges.add("Evil Be Gone")
                        session.eyes_active = False
                        session.entities_survived.add("Eyes")
                        await self.send_tracked(
                            session,
                            "✝️ The Crucifix illuminates the room! **Eyes** is engulfed in blue chains and banished!"
                        )
                        break

                    session.health -= 10
                    if session.health <= 0:
                        await self.kill_session(session, "💀 You stared at Eyes for too long.", "Eyes")
                        break
                    await self.send_tracked(
                        session,
                        f"👁️ **Eyes drains 10 HP!** Don't look at it! Health: **{session.health}/100**"
                    )
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
                await self.send_tracked(
                    session,
                    "**Psst!** 👂 You hear a whisper right by your ear!\n"
                    "Quick, turn around!",
                    view=ScreechView(self, session),
                )
            qte_time = 2.0 + (2.0 if "vitamins" in session.inventory else 0.0)
            await asyncio.sleep(qte_time)
            async with session.lock:
                if session.screech_active:
                    if "crucifix" in session.inventory:
                        session.inventory.remove("crucifix")
                        session.crucifix_used = True
                        session.badges.add("Evil Be Gone")
                        session.screech_active = False
                        session.entities_survived.add("Screech")
                        await self.send_tracked(session, "✝️ The Crucifix activates! **Screech** is blasted into the void.")
                    else:
                        session.health -= 40
                        session.screech_active = False
                        if session.health <= 0:
                            await self.kill_session(session, "💀 Screech attacked you in the pitch dark.", "Screech")
                        else:
                            await self.send_tracked(
                                session,
                                f"💀 **SCREECH BITES YOU!** You take 40 damage. Health: **{session.health}/100**"
                            )
        except asyncio.CancelledError:
            return

    async def halt_timeout(self, session: GameSession, expected_step: int) -> None:
        try:
            qte_time = 2.0 + (2.0 if "vitamins" in session.inventory else 0.0)
            await asyncio.sleep(qte_time)
            async with session.lock:
                if session.halt_active and session.halt_step == expected_step:
                    if "crucifix" in session.inventory:
                        session.inventory.remove("crucifix")
                        session.crucifix_used = True
                        session.badges.add("Evil Be Gone")
                        session.halt_active = False
                        session.entities_survived.add("Halt")
                        await self.send_tracked(session, "✝️ The Crucifix activates! **Halt** is banished from the hallway.")
                    else:
                        await self.kill_session(session, "💀 You hesitated in Halt's hallway.", "Halt")
        except asyncio.CancelledError:
            return

    async def roll_room_event(self, session: GameSession) -> None:
        roll = random.randint(1, 100)

        # Halt (Doors 55-65)
        if 55 <= session.current_door <= 65 and roll <= 20:
            session.halt_active = True
            session.halt_step = 0
            session.halt_expected = "TURN AROUND"
            session.halt_task = asyncio.create_task(self.halt_timeout(session, 0))
            await self.send_tracked(
                session,
                "🔵 **HALT CHASE**\nThe corridor turns electric blue. A distorted silhouette appears ahead...",
                view=HaltView(self, session, "TURN AROUND"),
            )
            return

        # Eyes (Doors 53+)
        if session.current_door >= 53 and 21 <= roll <= 38:
            session.eyes_active = True
            session.eyes_task = asyncio.create_task(self.eyes_timeout(session))
            await self.send_tracked(
                session,
                "👁️ **LOOK AWAY!**\nA blinding purple aurora and clicking sound fills the room! Click the button immediately!",
                view=EyesView(self, session),
            )
            return

        # Dark Room / Screech
        is_dark = (90 <= session.current_door <= 98) or (39 <= roll <= 58)
        if is_dark:
            await self.send_tracked(session, "🌑 **The lights are broken. The room is pitch black.**")
            if "flashlight" not in session.inventory:
                if random.randint(1, 100) <= 60:
                    session.screech_active = True
                    session.screech_task = asyncio.create_task(self.screech_timeout(session))
            else:
                await self.send_tracked(session, "🔦 Your Flashlight illuminates the gloom, keeping Screech away.")

        # Greenhouse Logic (Doors 90-98)
        if 90 <= session.current_door <= 98:
            if random.random() < 0.25:
                session.snared_until = clock() + 3.0
                await self.send_tracked(session, "🌿 **SNAP!** You stepped on a Snare! You are trapped for 3 seconds.")

            if random.randint(1, 100) <= 30:
                await self.start_threat(session, "rush", silent=True)
                return

        # Normal threats (Doors 10+)
        if session.current_door < 90:
            if roll <= 10:
                await self.start_threat(session, "ambush")
            elif roll <= 22:
                await self.start_threat(session, "rush")

    async def start_threat(self, session: GameSession, kind: str, silent: bool = False) -> None:
        threat = ThreatState(kind=kind, required_closet_uses=3 if kind == "ambush" else 1)
        session.threat = threat

        if kind == "ambush":
            prompt = (
                "⚠️ **ALERT: AMBUSH is speeding through!**\n"
                "🚪 Closets nearby. Hide immediately using `/hide` or `/closet`!\n"
                "*(Remember: Ambush rebounds multiple times!)*"
            )
        else:
            if silent:
                prompt = (
                    "🔊 *(You hear a low, sinister roaring getting closer in the dark...)*\n"
                    "There are no flickering lights here! Hide in a closet using `/hide` or `/closet` quickly!"
                )
            else:
                prompt = (
                    "⚠️ **ALERT: The lights flicker violently! RUSH arrives in 5 seconds!**\n"
                    "Quick! Hide in a closet using `/hide` or `/closet`!"
                )
        await self.send_tracked(session, prompt)
        threat.task = asyncio.create_task(self.threat_timeout(session))

    async def threat_timeout(self, session: GameSession) -> None:
        try:
            reaction_time = THREAT_SECONDS + (2.0 if "vitamins" in session.inventory else 0.0)
            await asyncio.sleep(reaction_time)
            async with session.lock:
                if session.threat:
                    if "crucifix" in session.inventory:
                        session.inventory.remove("crucifix")
                        session.crucifix_used = True
                        session.badges.add("Evil Be Gone")
                        entity = session.threat.kind.capitalize()
                        session.entities_survived.add(entity)
                        session.threat = None
                        await self.send_tracked(
                            session,
                            f"✝️ The Crucifix bursts with holy light! **{entity}** is banished to the ground."
                        )
                    else:
                        await self.kill_session(
                            session,
                            f"💀 You were caught in the open. {session.threat.kind.capitalize()} ripped through the room.",
                            session.threat.kind.capitalize(),
                        )
        except asyncio.CancelledError:
            return

    async def seek_timeout(self, session: GameSession, expected_index: int, chase_type: int = 1) -> None:
        try:
            base_delay = 3.0 if chase_type == 2 else QTE_SECONDS
            delay = base_delay + (2.0 if "vitamins" in session.inventory else 0.0)
            await asyncio.sleep(delay)
            async with session.lock:
                if session.seek and session.seek.index == expected_index:
                    if "crucifix" in session.inventory:
                        session.inventory.remove("crucifix")
                        session.crucifix_used = True
                        session.badges.add("Evil Be Gone")
                        session.entities_survived.add("Seek")
                        session.seek = None
                        await self.send_tracked(session, "✝️ The Crucifix activates! **Seek** is banished. The chase ends.")
                    else:
                        await self.kill_session(
                            session,
                            "💀 You hesitated for too long. Seek caught you in the hallway.",
                            "Seek",
                        )
        except asyncio.CancelledError:
            return

    async def heartbeat_timeout(self, session: GameSession, expected_index: int) -> None:
        try:
            delay = QTE_SECONDS + (2.0 if "vitamins" in session.inventory else 0.0)
            await asyncio.sleep(delay)
            async with session.lock:
                if session.heartbeat and session.heartbeat.index == expected_index:
                    await self.kill_session(
                        session,
                        "💀 You lost your breathing rhythm. Figure heard your panic and lunged.",
                        "Figure",
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
        cancel_task(session.eyes_task)
        cancel_task(session.screech_task)
        cancel_task(session.halt_task)
        session.threat = None
        session.seek = None
        session.heartbeat = None
        session.look_task = None
        session.hide_task = None
        session.eyes_task = None
        session.screech_task = None
        session.halt_task = None

        channel = session.last_channel
        if channel:
            embed = discord.Embed(
                title="Guiding Light",
                description=f"{reason}\n\n**Run ended at Door {session.current_door}.**\nUse `/start` to try again.",
                color=0x2B82D9,
            )
            hints = {
                "Rush": "When the lights flicker, find a closet and hide immediately.",
                "Ambush": "Ambush rebounds repeatedly! Exit the closet and jump back in after each pass.",
                "Seek": "Follow the guiding light during the chase. Vitamins can give you extra time!",
                "Figure": "Figure is blind but hears footsteps and heartbeats. Crouch quietly.",
                "Timothy": "Timothy lurks in drawers when looting. Keep your health high.",
                "Dupe": "Pay attention to the number of the door you just came through. Messages get cleared on /next!",
                "Hide": "You cannot stay in closets forever. Hide will push you out after 8 seconds!",
                "Eyes": "When a purple glow appears, do NOT look! Press [LOOK AWAY] immediately.",
                "Screech": "Listen for the 'Psst!' in dark rooms and turn around, or carry a Flashlight.",
                "Halt": "In the blue hallway, watch for the screen to flash and follow the direction prompts.",
                "Snare": "Watch the ground in the Greenhouse! Snares trap you while entities approach.",
            }
            hint_text = hints.get(entity_hint, "Learn from your mistakes and listen closely to audio cues.")
            embed.add_field(name="Canonical Advice", value=f"*{hint_text}*")
            msg = await channel.send(embed=embed)
            session.room_messages.append(msg)

    @app_commands.command(name="start", description="Start a new DOORS Floor 1 run.")
    async def start_command(self, interaction: discord.Interaction) -> None:
        key = (interaction.guild_id or 0, interaction.user.id)
        old = self.sessions.get(key)
        if old:
            await self.clear_room_messages(old)
            cancel_task(old.threat.task if old.threat else None)
            cancel_task(old.seek.task if old.seek else None)
            cancel_task(old.heartbeat.task if old.heartbeat else None)
            cancel_task(old.look_task)
            cancel_task(old.hide_task)
            cancel_task(old.eyes_task)
            cancel_task(old.screech_task)
            cancel_task(old.halt_task)

        session = GameSession(
            guild_id=interaction.guild_id or 0,
            user_id=interaction.user.id,
            last_channel=interaction.channel,
        )
        self.sessions[key] = session
        await interaction.response.send_message(
            "🚪 **DOORS — Floor 1 Hotel**\n"
            "You unlock the lobby doors and step inside. Reach Door 100 alive.\n"
            "Use `/next` (1–2 doors) to move. Use `/loot` for gold. Use `/hide` when danger nears.\n"
            "💡 *Tip: Messages from past rooms are cleared on `/next` to make Dupe authentic! Remember your door numbers.*"
        )
        try:
            orig = await interaction.original_response()
            session.room_messages.append(orig)
        except Exception:
            pass

    @app_commands.command(name="next", description="Advance 1–2 doors (clears previous room messages).")
    @app_commands.describe(amount="Number of doors to advance (1–2).")
    async def next_command(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 2] = 1
    ) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return

        async with session.lock:
            if session.busy or session.looking_around or session.searching_book:
                await interaction.response.send_message(
                    "Finish the active encounter or minigame before opening the next door.", ephemeral=True
                )
                return

            now = clock()
            if now < session.snared_until:
                remaining = session.snared_until - now
                await interaction.response.send_message(
                    f"🌿 **You are caught in a Snare!** Wait {remaining:.1f}s to break free.",
                    ephemeral=True,
                )
                return

            elapsed = now - session.last_next_at
            if elapsed < COOLDOWN_SECONDS:
                await interaction.response.send_message(
                    f"⏳ The door handle needs a moment. Try again in {COOLDOWN_SECONDS - elapsed:.1f}s.",
                    ephemeral=True,
                )
                return
            session.last_next_at = now

            if session.locked and not session.key_found:
                await interaction.response.send_message(
                    "🔒 This door is locked. Use `/look_around` to search the room for the key.",
                    ephemeral=True,
                )
                return

            if session.locked:
                session.locked = False
                session.key_found = False
                session.inventory.discard("room_key")

            # CLEAR PAST ROOM MESSAGES:
            # This deletes the previous room text so players cannot scroll up to cheat on Dupe!
            await self.clear_room_messages(session)

            target = session.current_door + amount

            # Enforce mandatory stops
            if session.current_door < 50 and target > 50:
                target = 50
            elif session.current_door < 52 and target > 52:
                target = 52  # Door 52 Jeff's Shop is mandatory!
            elif session.current_door < 70 and target > 70:
                target = 70  # Seek Chase #2
            elif session.current_door < 100 and target > 100:
                target = 100

            # Door 100 Finale
            if target >= 100:
                session.current_door = 100
                session.door100_phase = 1
                session.door100_code = "".join(random.choice(["0", "1"]) for _ in range(10))
                session.door100_switches = [False] * 10
                await interaction.response.send_message(
                    "🚪 **Door 100: The Electrical Breaker Room**\n"
                    "You enter the vast concrete industrial warehouse. The elevator sits powered down.\n"
                    "At the far end of the gantry sits the main breaker lever.\n"
                    "Press **[PULL LEVER]** to begin the power sequence!",
                    view=FigureEncounterView(self, session),
                )
                try:
                    orig = await interaction.original_response()
                    session.room_messages.append(orig)
                except Exception:
                    pass
                return

            # Dupe Hallway (chance on moving 2 doors)
            if amount == 2 and session.current_door >= 10 and random.random() < 0.35:
                correct = target
                session.dupe = DupeChallenge(correct_door=correct, wrong_door=correct - 1)
                await interaction.response.send_message(
                    "🚪 **DUPE HALLWAY**\n"
                    "Two doors stand before you in the gloom.\n"
                    f"One is marked **Door {correct}**, the other **Door {correct - 1}**.\n"
                    "Use `/door number` to choose. Picking the fake door deals **40 damage**!\n"
                    "*(Past room messages were cleared — did you remember your door number?)*",
                )
                try:
                    orig = await interaction.original_response()
                    session.room_messages.append(orig)
                except Exception:
                    pass
                return

            session.current_door = target

            # Door 50 Library
            if session.current_door == 50:
                session.in_library = True
                session.library_code = "".join(str(random.randint(0, 9)) for _ in range(5))
                await interaction.response.send_message(
                    "📚 **Door 50: The Library**\n"
                    "A gigantic two-story library smells of decaying paper. Figure patrols the floor.\n"
                    "It is completely blind, but hears every sound.\n"
                    "Collect 5 book fragments using `/search_book`. Crouch with `/crouch` if it approaches.",
                )
                try:
                    orig = await interaction.original_response()
                    session.room_messages.append(orig)
                except Exception:
                    pass
                return

            # Door 52 Jeff's Shop (Mandatory stop)
            if session.current_door == 52:
                await interaction.response.send_message(
                    "🛒 **Door 52: Jeff's Shop**\n"
                    "Soothing elevator music plays. Jeff the tentacle shopkeeper waves from behind the counter.\n"
                    "El Goblino sits on a crate nearby. Use the buttons below to purchase gear, or `/talk` to hear tips!\n"
                    f"Your Wallet: **{session.wallet}g**",
                    view=JeffShopView(self, session),
                )
                try:
                    orig = await interaction.original_response()
                    session.room_messages.append(orig)
                except Exception:
                    pass
                return

            # Door 53 Infirmary (Secret room with Skeleton Key)
            if session.current_door == 53:
                if "skeleton_key" in session.inventory and not session.infirmary_unlocked:
                    await interaction.response.send_message(
                        "🏥 **Door 53: The Infirmary**\n"
                        "A reinforced gate marked with a skull padlock guards the medical ward.\n"
                        "You hold a **Skeleton Key**! You can unlock the ward to fully heal and find supplies.",
                        view=InfirmaryView(self, session),
                    )
                    try:
                        orig = await interaction.original_response()
                        session.room_messages.append(orig)
                    except Exception:
                        pass
                    return

            # Seek Chases (Door 30 or Door 70)
            if session.current_door in SEEK_DOORS:
                await interaction.response.defer()
                await self.start_seek(session)
                return

            await interaction.response.defer()
            await self.send_room_entry(session)

    async def start_seek(self, session: GameSession) -> None:
        chase_type = 2 if session.current_door >= 70 else 1
        options = ["LEFT", "RIGHT", "DUCK", "AVOID"] if chase_type == 2 else ["LEFT", "RIGHT", "CROUCH"]
        sequence = [random.choice(options) for _ in range(7 if chase_type == 2 else 5)]
        challenge = SeekChallenge(sequence=sequence)
        session.seek = challenge
        challenge.task = asyncio.create_task(self.seek_timeout(session, 0, chase_type=chase_type))

        wait_time = (3.0 if chase_type == 2 else QTE_SECONDS) + (2.0 if "vitamins" in session.inventory else 0.0)
        first_display = {
            "LEFT": "⬅️ LEFT",
            "RIGHT": "➡️ RIGHT",
            "CROUCH": "🫥 CROUCH",
            "DUCK": "🦆 DUCK UNDER FALLEN BEAM",
            "AVOID": "✋ AVOID REACHING HANDS",
        }.get(sequence[0], sequence[0])

        title = "🔥 **SEEK CHASE #2 — THE BURNING HALLWAY**" if chase_type == 2 else "👁️ **SEEK CHASE INITIATED**"
        msg_text = (
            f"{title}\n"
            "Seek erupts from the floor in a surge of black slime!\n"
            f"🔵 Guiding Light marks your first path: **{first_display}**\n"
            f"Click the matching button within **{wait_time:.1f} seconds**!"
        )
        challenge.message = await self.send_tracked(
            session,
            msg_text,
            view=SeekView(self, session, chase_type=chase_type),
        )

    @app_commands.command(name="look_around", description="Search a locked room for its key.")
    async def look_around_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return

        async with session.lock:
            if not session.locked:
                await interaction.response.send_message("This door is not locked.", ephemeral=True)
                return
            if session.key_found:
                await interaction.response.send_message(
                    "🔑 You already found the key. Use `/next 1` to unlock and advance.", ephemeral=True
                )
                return
            if session.looking_around:
                await interaction.response.send_message("You are already rummaging through drawers...", ephemeral=True)
                return
            session.looking_around = True
            await interaction.response.send_message("🔎 You search through desk drawers and shelves (5–7 seconds)...")

        try:
            await asyncio.sleep(random.randint(5, 7))
            async with session.lock:
                if session.dead or not session.locked:
                    return
                session.key_found = True
                session.inventory.add("room_key")
                await self.send_tracked(
                    session,
                    f"🔑 **Key Discovered!** You found the key behind a bookshelf. Use `/next 1` to proceed."
                )
        finally:
            session.looking_around = False

    @app_commands.command(name="loot", description="Collect coins in the room (10–50g, 5% Timothy chance).")
    async def loot_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return

        async with session.lock:
            if session.coins_amount <= 0:
                await interaction.response.send_message("There is no loot left in this room.", ephemeral=True)
                return

            amount = session.coins_amount
            session.wallet += amount
            session.coins_amount = 0

            # 5% Timothy jumpscare
            if random.random() < 0.05:
                session.health -= 5
                if session.health <= 0:
                    await interaction.response.defer()
                    await self.kill_session(session, "🕷️ Timothy bit you from inside the drawer.", "Timothy")
                    return
                await interaction.response.send_message(
                    f"🪙 You collected **{amount} gold**.\n"
                    f"🕷️ **TIMOTHY JUMPS OUT!** A hairy spider lunges at your face! You take 5 damage. Health: **{session.health}/100**"
                )
            else:
                await interaction.response.send_message(
                    f"🪙 You collected **{amount} gold**! Wallet: **{session.wallet}g**"
                )
            try:
                orig = await interaction.original_response()
                session.room_messages.append(orig)
            except Exception:
                pass

    async def hide_timeout(self, session: GameSession) -> None:
        try:
            await asyncio.sleep(SAFE_HIDE_SECONDS)
            async with session.lock:
                if session.hiding:
                    session.hiding = False
                    session.health -= 40
                    session.hide_task = None
                    if session.health <= 0:
                        await self.kill_session(session, "💀 Hide forcefully threw you out of the closet.", "Hide")
                    else:
                        await self.send_tracked(
                            session,
                            f"💀 **HIDE ATTACKS!** You stayed in the closet for over 8 seconds!\n"
                            f"Hide violently ejects you for 40 damage. Health: **{session.health}/100**"
                        )
        except asyncio.CancelledError:
            return

    @app_commands.command(name="hide", description="Hide in a closet (safe up to 8s).")
    async def hide_command(self, interaction: discord.Interaction) -> None:
        await self._do_hide(interaction)

    @app_commands.command(name="closet", description="Hide in a closet (alias for /hide).")
    async def closet_command(self, interaction: discord.Interaction) -> None:
        await self._do_hide(interaction)

    async def _do_hide(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return

        async with session.lock:
            if session.hiding:
                await interaction.response.send_message("You are already hiding in a closet.", ephemeral=True)
                return

            now = clock()
            if now < session.snared_until:
                await interaction.response.send_message("🌿 You are caught in a Snare and cannot reach the closet!", ephemeral=True)
                return

            session.hiding = True
            session.hide_task = asyncio.create_task(self.hide_timeout(session))

            if session.threat:
                threat = session.threat
                threat.closet_uses += 1
                if threat.kind == "rush":
                    cancel_task(threat.task)
                    session.threat = None
                    session.entities_survived.add("Rush")
                    await interaction.response.send_message(
                        "🚪 You yank the closet shut. Rush roars past, shattering every lightbulb before fading into silence.",
                        view=ClosetView(self, session),
                    )
                elif threat.closet_uses >= threat.required_closet_uses:
                    cancel_task(threat.task)
                    session.threat = None
                    session.entities_survived.add("Ambush")
                    await interaction.response.send_message(
                        "🚪 Ambush screams past for the final time. It gives up and retreats into the darkness!",
                        view=ClosetView(self, session),
                    )
                else:
                    await interaction.response.send_message(
                        f"🚪 Ambush screams past, but turns around! Quickly exit and re-enter!\n"
                        f"Passes survived: **{threat.closet_uses}/{threat.required_closet_uses}**",
                        view=ClosetView(self, session),
                    )
            else:
                await interaction.response.send_message(
                    "🚪 You slip into a closet. Keep an ear out—staying longer than 8s will anger Hide!",
                    view=ClosetView(self, session),
                )
            try:
                orig = await interaction.original_response()
                session.room_messages.append(orig)
            except Exception:
                pass

    @app_commands.command(name="door", description="Choose a door number in a Dupe hallway.")
    @app_commands.describe(number="The door number to enter.")
    async def door_command(self, interaction: discord.Interaction, number: int) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return

        async with session.lock:
            if not session.dupe:
                await interaction.response.send_message("There is no Dupe hallway here.", ephemeral=True)
                return

            challenge = session.dupe
            session.dupe = None

            # Clear Dupe prompt message
            await self.clear_room_messages(session)

            if number != challenge.correct_door:
                session.health -= 40
                if session.health <= 0:
                    await interaction.response.defer()
                    await self.kill_session(session, "💀 You opened the wrong door. Dupe mauled you.", "Dupe")
                    return
                await interaction.response.send_message(
                    f"❌ **DUPE ATTACKS!** The door slammed shut on your face dealing 40 damage!\n"
                    f"Health: **{session.health}/100**. The genuine door was **Door {challenge.correct_door}**. Proceeding..."
                )
                try:
                    orig = await interaction.original_response()
                    session.room_messages.append(orig)
                except Exception:
                    pass
                session.current_door = challenge.correct_door
                await self.send_room_entry(session)
                return

            session.current_door = challenge.correct_door
            await interaction.response.send_message(f"✅ Correct! Door {challenge.correct_door} opens safely.")
            try:
                orig = await interaction.original_response()
                session.room_messages.append(orig)
            except Exception:
                pass
            await self.send_room_entry(session)

    @app_commands.command(name="search_book", description="Search the Library for a code fragment (Door 50).")
    async def search_book_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return

        async with session.lock:
            if not session.in_library or session.current_door != 50:
                await interaction.response.send_message("Books can only be searched inside Door 50 Library.", ephemeral=True)
                return
            if session.books_found >= 5:
                await interaction.response.send_message("You found all 5 books! Use `/crack_code [code]` to open the door.", ephemeral=True)
                return
            if session.searching_book or session.heartbeat:
                await interaction.response.send_message("You are currently focused on a minigame.", ephemeral=True)
                return
            session.searching_book = True
            await interaction.response.defer()

        try:
            await asyncio.sleep(random.uniform(1.2, 2.2))
            async with session.lock:
                if session.dead:
                    return
                # 35% chance of triggering Heartbeat QTE
                if random.random() < 0.35:
                    seq = [random.choice(["RED", "BLUE", "GREEN"]) for _ in range(3)]
                    challenge = HeartbeatChallenge(sequence=seq)
                    session.heartbeat = challenge
                    challenge.task = asyncio.create_task(self.heartbeat_timeout(session, 0))
                    shown = " ".join(self.heartbeat_emoji[c] for c in seq)
                    qte_time = QTE_SECONDS + (2.0 if "vitamins" in session.inventory else 0.0)
                    msg = await interaction.followup.send(
                        "💓 **HEARTBEAT MINIGAME**\n"
                        "Figure steps nearby! Match the heartbeat rhythm:\n"
                        f"{shown}\n"
                        f"First color: **{seq[0]}** (You have {qte_time:.1f}s)",
                        view=HeartbeatView(self, session),
                    )
                    if msg:
                        session.room_messages.append(msg)
                else:
                    session.books_found += 1
                    fragment = session.library_code[session.books_found - 1]
                    msg = await interaction.followup.send(
                        f"📖 You found a glowing Library Book! Code fragment **{session.books_found}/5**: "
                        f"Digit **#{session.books_found} = {fragment}**\n"
                        f"Books collected: **{session.books_found}/5**"
                    )
                    if msg:
                        session.room_messages.append(msg)
        finally:
            session.searching_book = False

    @app_commands.command(name="search_switches", description="Search for electrical breaker switches in Door 100.")
    async def search_switches_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return

        async with session.lock:
            if session.door100_phase < 2:
                await interaction.response.send_message("Pull the breaker lever first.", ephemeral=True)
                return
            if session.hiding:
                await interaction.response.send_message("You are hiding in a closet. Exit first using the button.", ephemeral=True)
                return
            if session.door100_phase >= 3:
                await interaction.response.send_message("All 10 switches are collected! Solve the breaker puzzle.", ephemeral=True)
                return

            await interaction.response.defer()

        await asyncio.sleep(random.uniform(1.2, 2.0))
        async with session.lock:
            if session.dead:
                return

            # Figure patrolling heartbeat encounter
            if random.random() < 0.30:
                seq = [random.choice(["RED", "BLUE", "GREEN"]) for _ in range(3)]
                challenge = HeartbeatChallenge(sequence=seq)
                session.heartbeat = challenge
                challenge.task = asyncio.create_task(self.heartbeat_timeout(session, 0))
                shown = " ".join(self.heartbeat_emoji[c] for c in seq)
                msg = await interaction.followup.send(
                    "💓 **FIGURE DETECTS YOU!**\n"
                    "Match the heartbeats to stay silent:\n"
                    f"{shown}\nFirst color: **{seq[0]}**",
                    view=HeartbeatView(self, session),
                )
                if msg:
                    session.room_messages.append(msg)
                return

            session.switches_found += 1
            if session.switches_found >= 10:
                session.door100_phase = 3
                target_str = " | ".join(
                    f"{i+1}:{'ON' if c == '1' else 'OFF'}" for i, c in enumerate(session.door100_code)
                )
                msg = await interaction.followup.send(
                    f"⚡ **ALL 10 BREAKER SWITCHES COLLECTED! (10/10)**\n"
                    f"**TARGET CODE:** `{target_str}`\n"
                    "Toggle the 10 switches to match the code, then click **[⚡ SUBMIT CODE]**!",
                    view=BreakerPuzzleView(self, session),
                )
                if msg:
                    session.room_messages.append(msg)
            else:
                msg = await interaction.followup.send(
                    f"⚡ You grabbed a circuit breaker switch from a metal shelf! Progress: **{session.switches_found}/10**"
                )
                if msg:
                    session.room_messages.append(msg)

    @app_commands.command(name="crack_code", description="Enter the 5-digit Library exit code (Door 50).")
    @app_commands.describe(code="The 5-digit code.")
    async def crack_code_command(self, interaction: discord.Interaction, code: str) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return

        async with session.lock:
            if not session.in_library:
                await interaction.response.send_message("There is no code lock here.", ephemeral=True)
                return
            if session.books_found < 5:
                await interaction.response.send_message(
                    f"You need all 5 book fragments first ({session.books_found}/5).", ephemeral=True
                )
                return
            if code != session.library_code:
                await interaction.response.defer()
                await self.kill_session(session, "💀 The lock buzzer sounded loudly. Figure caught you at the door.", "Figure")
                return

            session.current_door = 51
            session.in_library = False
            await interaction.response.send_message(
                "🔓 **BEEP-CLICK!** The heavy padlock snaps open! You slip through into Door 51 and bolt the door behind you."
            )
            try:
                orig = await interaction.original_response()
                session.room_messages.append(orig)
            except Exception:
                pass

    @app_commands.command(name="talk", description="Talk to El Goblino at Door 52 (Jeff's Shop).")
    async def talk_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return

        async with session.lock:
            if session.current_door != 52:
                await interaction.response.send_message("El Goblino is only hanging out at Door 52.", ephemeral=True)
                return

            tips = [
                "Hey buddy! If you got gold, grab that Crucifix. It'll rip any of these hotel freaks straight into the ground!",
                "In dark rooms, keep your ears perked for a little 'Psst!'. Look behind you instantly, or keep a Flashlight glowing!",
                "Past Door 90, the Greenhouse has no flickering lights. Rush will sneak up on you dead silent!",
                "Figure is blind as a bat, but he's got super hearing. Crouch down and hold your breath!",
                "That Skeleton Key? Jeff says it unlocks the old Infirmary past Door 52. Lots of medicine in there!",
                "When you see Eyes' purple glow, DO NOT LOOK AT IT. Click Look Away immediately!",
                "Pay attention to the door numbers! Dupe loves messing with people who don't memorize the door they just came from.",
            ]
            await interaction.response.send_message(f"👹 **El Goblino:** \"{random.choice(tips)}\"")
            try:
                orig = await interaction.original_response()
                session.room_messages.append(orig)
            except Exception:
                pass

    @app_commands.command(name="crouch", description="Crouch quietly in the Library (Door 50).")
    async def crouch_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        await interaction.response.send_message("🫥 You crouch behind the bookshelves. The heavy footsteps thud past.", ephemeral=False)
        try:
            orig = await interaction.original_response()
            session.room_messages.append(orig)
        except Exception:
            pass

    @app_commands.command(name="left", description="Choose LEFT during Seek chase (fallback).")
    async def left_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session and session.seek:
            view = SeekView(self, session)
            await view.handle_action(interaction, "LEFT")
        else:
            await interaction.response.send_message("No active Seek chase.", ephemeral=True)

    @app_commands.command(name="right", description="Choose RIGHT during Seek chase (fallback).")
    async def right_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session and session.seek:
            view = SeekView(self, session)
            await view.handle_action(interaction, "RIGHT")
        else:
            await interaction.response.send_message("No active Seek chase.", ephemeral=True)

    @app_commands.command(name="status", description="Inspect your current run stats and inventory.")
    async def status_command(self, interaction: discord.Interaction) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return

        async with session.lock:
            items = ", ".join(sorted(session.inventory)) if session.inventory else "None"
            badges = ", ".join(sorted(session.badges)) if session.badges else "None"
            active_threat = "None"
            if session.threat:
                active_threat = session.threat.kind.capitalize()
            elif session.seek:
                active_threat = "Seek Chase"
            elif session.eyes_active:
                active_threat = "Eyes"
            elif session.screech_active:
                active_threat = "Screech"
            elif session.halt_active:
                active_threat = "Halt"

            await interaction.response.send_message(
                f"🚪 **Current Door:** {session.current_door}/100\n"
                f"❤️ **Health:** {session.health}/100\n"
                f"🪙 **Gold Wallet:** {session.wallet}g\n"
                f"🎒 **Inventory:** {items}\n"
                f"⚠️ **Active Threat:** {active_threat}\n"
                f"🏅 **Badges Earned:** {badges}",
                ephemeral=True,
            )

    @app_commands.command(name="doors_help", description="Show DOORS command list and survival guide.")
    async def doors_help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="📖 DOORS Floor 1 Survival Manual",
            description=(
                "`/start` — Start or restart a Floor 1 run\n"
                "`/next [1-2]` — Advance 1–2 doors (clears past room messages)\n"
                "`/loot` — Collect coins (10–50g, 5% Timothy chance)\n"
                "`/hide` or `/closet` — Hide in a closet (safe up to 8s max!)\n"
                "`/look_around` — Search locked rooms for the key (5–7s)\n"
                "`/talk` — Talk to El Goblino at Jeff's Shop (Door 52)\n"
                "`/search_book` — Search Door 50 Library for 5 code fragments\n"
                "`/crack_code [code]` — Unlock Door 50 exit\n"
                "`/search_switches` — Search Door 100 for 10 breaker switches\n"
                "`/door [number]` — Choose door in Dupe hallways\n"
                "`/crouch` — Crouch quietly in the Library\n"
                "`/status` — View your health, inventory, and badges\n"
                "`/reset` — Reset current game"
            ),
            color=0x2B82D9,
        )
        embed.set_footer(text="Notice: Past room messages get deleted on /next so Dupe cannot be cheated!")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="reset", description="Delete and reset your active run.")
    async def reset_command(self, interaction: discord.Interaction) -> None:
        key = (interaction.guild_id or 0, interaction.user.id)
        session = self.sessions.pop(key, None)
        if session:
            await self.clear_room_messages(session)
            cancel_task(session.threat.task if session.threat else None)
            cancel_task(session.seek.task if session.seek else None)
            cancel_task(session.heartbeat.task if session.heartbeat else None)
            cancel_task(session.look_task)
            cancel_task(session.hide_task)
            cancel_task(session.eyes_task)
            cancel_task(session.screech_task)
            cancel_task(session.halt_task)
        await interaction.response.send_message("🧹 Your DOORS run has been reset. Use `/start` to begin anew.", ephemeral=True)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Provide your Discord Bot token in the environment or .env."
        )
    bot = DoorsBot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
