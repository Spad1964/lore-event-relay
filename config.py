import os
from dataclasses import dataclass
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass
class TargetGuild:
    guild_id: int
    reminder_channel_id: Optional[int]


@dataclass
class Config:
    master_guild_id: int
    target_guilds: list[TargetGuild]
    event_name_prefix: str
    reminder_message: str
    log_level: str

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        raw_master = os.getenv("MASTER_GUILD_ID", "").strip()
        if not raw_master:
            raise ValueError("MASTER_GUILD_ID not set in .env")

        target_guilds = [
            TargetGuild(
                guild_id=int(g["guild_id"]),
                reminder_channel_id=int(g["reminder_channel_id"])
                if g.get("reminder_channel_id")
                else None,
            )
            for g in (data.get("target_guilds") or [])
        ]

        return cls(
            master_guild_id=int(raw_master),
            target_guilds=target_guilds,
            event_name_prefix=data.get("event_name_prefix", "[RELAY] "),
            reminder_message=data.get(
                "reminder_message",
                "⏰ O evento **{event_name}** começa em 30 minutos! {mentions}",
            ),
            log_level=data.get("log_level", "INFO"),
        )

    def get_target_guild(self, guild_id: int) -> Optional[TargetGuild]:
        return next(
            (g for g in self.target_guilds if g.guild_id == guild_id), None
        )
