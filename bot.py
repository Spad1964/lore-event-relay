import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import Config
from database import Database

load_dotenv()

log = logging.getLogger(__name__)


def setup_logging(log_level: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler("bot.log", encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Reduce noise from discord internals
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


class LoreRelayBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.guild_scheduled_events = True
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        self.config = config
        self.db = Database("events.db")

    async def setup_hook(self) -> None:
        await self.db.init()

        for cog in ("cogs.relay", "cogs.reminders", "cogs.admin"):
            await self.load_extension(cog)

        # Sync slash commands globally
        await self.tree.sync()
        log.info("Slash commands synced")

    async def on_ready(self) -> None:
        log.info(f"Bot online: {self.user} (ID: {self.user.id})")
        log.info(f"Master guild: {self.config.master_guild_id}")
        log.info(
            f"Target guilds: {[g.guild_id for g in self.config.target_guilds]}"
        )

        # Startup sync: create missing relays for events that appeared while offline
        relay_cog = self.get_cog("RelayEvents")
        if relay_cog:
            await relay_cog.startup_sync()

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        log.exception(f"Unhandled error in {event_method}")


async def main() -> None:
    try:
        config = Config.load()
    except Exception as e:
        print(f"ERROR loading config: {e}")
        sys.exit(1)

    setup_logging(config.log_level)

    token = os.getenv("BOT_TOKEN")
    if not token:
        log.critical("BOT_TOKEN not set in .env")
        sys.exit(1)

    async with LoreRelayBot(config) as bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
