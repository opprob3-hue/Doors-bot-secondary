# DOORS Discord Bot

A Python `discord.py` bot that runs concurrent, text-based DOORS Floor 1 sessions with timed threats and interactive QTEs.

## Run & Operate

- `python -m doors_bot.bot` — run the Discord bot
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required secret: `DISCORD_TOKEN` — Discord bot token

## Stack

- Python 3 with `discord.py`
- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `doors_bot/bot.py` — game state machine, slash commands, timers, and button QTEs
- `requirements.txt` — Python dependency
- `README.md` — Discord setup and run instructions

## Architecture decisions

- Sessions are keyed by `(guild_id, user_id)` so players can run concurrently without sharing state.
- Per-session asyncio locks protect transitions when a slash command and timeout happen together.
- Threat and QTE timers are cancellable tasks; a process restart intentionally resets active runs.

## Product

- Slash-command DOORS Floor 1 game with locked rooms, coins, Rush, Ambush, Dupe, Seek, and Figure.
- Interactive Discord buttons handle the strict Seek and heartbeat quick-time events.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
