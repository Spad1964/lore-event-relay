import os
from dataclasses import dataclass
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

DEFAULT_RELAY_FIELDS = frozenset({"name", "description", "end_time", "image", "location"})


def _validate_relay_fields(raw_fields: object, guild_id: int) -> list[str]:
    if not isinstance(raw_fields, list):
        raise ValueError(
            f"relay_fields for target guild {guild_id} must be a list of strings"
        )

    fields: list[str] = []
    seen_fields: set[str] = set()
    for raw_field in raw_fields:
        field = str(raw_field).strip().lower()
        if not field:
            continue
        if field not in DEFAULT_RELAY_FIELDS:
            allowed = ", ".join(sorted(DEFAULT_RELAY_FIELDS))
            raise ValueError(
                f"Unsupported relay field '{field}' for target guild {guild_id}; "
                f"allowed values are: {allowed}"
            )
        if field in seen_fields:
            continue
        seen_fields.add(field)
        fields.append(field)

    return fields


@dataclass
class TargetGuild:
    guild_id: int
    reminder_channel_id: Optional[int]
    relay_fields: Optional[list[str]] = None

    def relay_field_set(self) -> set[str]:
        if self.relay_fields is None:
            return set(DEFAULT_RELAY_FIELDS)
        return set(self.relay_fields)


@dataclass
class Config:
    master_guild_id: int
    target_guilds: list[TargetGuild]
    event_name_prefix: str
    reminder_message: str
    reminder_minutes_before: int
    log_level: str

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        raw_master = os.getenv("MASTER_GUILD_ID", "").strip()
        if not raw_master:
            raise ValueError("MASTER_GUILD_ID not set in .env")

        reminder_minutes_before = int(data.get("reminder_minutes_before", 30))
        if reminder_minutes_before <= 0:
            raise ValueError("reminder_minutes_before must be greater than 0")

        target_guilds = []
        seen_guilds: set[int] = set()
        for raw_guild in data.get("target_guilds") or []:
            guild_id = int(raw_guild["guild_id"])
            if guild_id in seen_guilds:
                raise ValueError(f"Duplicate target guild configured: {guild_id}")
            seen_guilds.add(guild_id)

            reminder_channel_id = (
                int(raw_guild["reminder_channel_id"])
                if raw_guild.get("reminder_channel_id")
                else None
            )

            relay_fields = None
            if "relay_fields" in raw_guild and raw_guild["relay_fields"] is not None:
                relay_fields = _validate_relay_fields(
                    raw_guild["relay_fields"],
                    guild_id,
                )

            target_guilds.append(
                TargetGuild(
                    guild_id=guild_id,
                    reminder_channel_id=reminder_channel_id,
                    relay_fields=relay_fields,
                )
            )

        return cls(
            master_guild_id=int(raw_master),
            target_guilds=target_guilds,
            event_name_prefix=data.get("event_name_prefix", "[PD] "),
            reminder_message=data.get(
                "reminder_message",
                "The event **{event_name}** starts in 5 minutes! {mentions}",
            ),
            reminder_minutes_before=reminder_minutes_before,
            log_level=data.get("log_level", "INFO"),
        )

    def get_target_guild(self, guild_id: int) -> Optional[TargetGuild]:
        return next(
            (g for g in self.target_guilds if g.guild_id == guild_id), None
        )
