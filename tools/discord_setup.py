"""Create the hiring server layout on Discord and store every id in the settings table. Idempotent: run it again and nothing is duplicated.

    .venv/bin/python -m tools.discord_setup            # reads .env next to the app (DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, HIRE_DB_PATH)
    .venv/bin/python -m tools.discord_setup --dry-run  # only prints what exists and what would be created (no writes)

Roles Manager / Applicant / Trainee; categories STAFF (hiring-queue, escalations, results, weekly-report, staff-chat), APPLICANTS (welcome,
applicant-lounge), FINALS (forum finals, voice "Mock Chat | Ian"); the welcome post; a permanent invite. Stops on 403 Missing Permissions.
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import dotenv_lite  # noqa: E402
dotenv_lite.load(os.path.join(HERE, ".env"))

import db  # noqa: E402
import channels  # noqa: E402


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    gid = int(os.environ.get("DISCORD_GUILD_ID", "0") or 0)
    if not token or not gid:
        print("DISCORD_BOT_TOKEN and DISCORD_GUILD_ID are needed in .env")
        return 2
    import discord
    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)
    result: dict = {}
    failure: list = []

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(gid) or await client.fetch_guild(gid)
            await guild.fetch_channels()
            if dry:
                print(f"guild {guild.name} ({guild.id}); administrator={guild.me.guild_permissions.administrator}")
                for r in guild.roles:
                    print(f"  role {r.name} {r.id}")
                for c in guild.channels:
                    print(f"  {c.type.name} {c.name} {c.id}")
                return
            with db.db() as con:
                db.init()
                result.update(await channels.setup_guild(guild, con, say=print))
        except discord.Forbidden as e:
            failure.append(f"403 Missing Permissions: {e}. The bot needs Administrator on the server; grant it and run again.")
        except PermissionError as e:
            failure.append(str(e))
        except Exception as e:  # noqa: BLE001
            failure.append(f"{type(e).__name__}: {e}")
        finally:
            await client.close()

    try:
        asyncio.run(client.start(token))
    except Exception as e:  # noqa: BLE001
        if not failure:
            failure.append(f"{type(e).__name__}: {e}")
    if failure:
        print(failure[0])
        return 1
    if result:
        print("\nDiscord layout (saved to settings):")
        for k, v in result.items():
            print(f"  {k} = {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
