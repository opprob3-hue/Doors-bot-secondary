# DOORS Discord Bot

A text-based DOORS Floor 1 run for Discord, built with Python and `discord.py`.
Each player gets an independent state machine per Discord server, so multiple
players can explore at the same time without sharing doors, inventory, timers,
or encounters.

## Included mechanics

- `/start`, `/next 1-3`, `/status`, and `/reset`
- Four-second `/next` cooldown
- Locked doors with a five-to-seven-second `/look_around` search
- Randomized room coins with `/collect` deleting the coin message
- Rush and Ambush, each with a five-second survival timer
- Dupe fake-door choice with 40 damage for the wrong door
- Seek chase at Door 30 and Door 80 with interactive buttons
- Figure's Door 50 Library with five code fragments, heartbeat color QTEs,
  and `/crack_code`
- In-memory per-player sessions with asyncio locks and cancellable tasks for
  concurrent timers

## Discord setup

1. In the Discord Developer Portal, create an application and add a bot.
2. Enable the `bot` scope and the `applications.commands` scope in the OAuth2
   URL generator.
3. Give the bot permission to:
   - View Channels
   - Send Messages
   - Manage Messages (needed to delete collected coin messages)
   - Use Slash Commands
4. Invite the bot to your server.

The project already expects the token as the `DISCORD_TOKEN` secret. Never
commit the token or place it directly in `doors_bot/bot.py`.

## Run on Replit

The recommended command is:

```bash
python -m doors_bot.bot
```

The project secret is automatically available as `DISCORD_TOKEN`. Keep the
process running; Discord slash commands are registered globally for both
servers and private messages when the bot connects. Global command updates can
take a short time to appear in Discord after a code change.

## Run locally

From the project directory:

```bash
python -m venv .venv
. .venv/bin/activate       # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
export DISCORD_TOKEN="your-bot-token"  # Windows PowerShell: $env:DISCORD_TOKEN="..."
export DISCORD_GUILD_ID="123456789012345678"  # optional, instant dev sync
python -m doors_bot.bot
```

Do not use a token copied into source control. If a token has ever been
committed or shared publicly, regenerate it in the Discord Developer Portal.

## Play flow

1. Run `/start`.
2. Use `/next 1` or `/next 2` to move carefully. `/next 3` can trigger Dupe.
3. Collect coins promptly with `/collect`.
4. At locked doors, run `/look_around`, wait for the key, then `/next 1`.
5. When Rush or Ambush appears, use `/closet` before five seconds expires.
6. At Seek, click the matching button within four seconds for every step.
7. At Door 50, run `/search_book` five times. Complete heartbeat buttons when
   Figure approaches, then combine the five revealed digits and run
   `/crack_code 12345`.

## Notes

Sessions are intentionally held in memory for fast concurrent gameplay. A
process restart resets active runs. The bot does not need privileged Discord
intents because it uses slash commands and component interactions.