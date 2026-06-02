import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)


def _is_meaningful_update(
    before: discord.ScheduledEvent, after: discord.ScheduledEvent
) -> bool:
    """Return True if any user-visible field changed (ignore status transitions)."""
    return (
        before.name != after.name
        or before.description != after.description
        or before.start_time != after.start_time
        or before.end_time != after.end_time
        or before.location != after.location
        or (before.cover_image and after.cover_image and before.cover_image.url != after.cover_image.url)
        or (before.cover_image is None) != (after.cover_image is None)
    )


class RelayEvents(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    def is_master_event(self, event: discord.ScheduledEvent) -> bool:
        return event.guild_id == self.bot.config.master_guild_id

    # ── Create relay ─────────────────────────────────────────────────────────

    async def create_relay_event(
        self,
        target_guild: discord.Guild,
        event: discord.ScheduledEvent,
    ) -> Optional[discord.ScheduledEvent]:
        name = f"{self.bot.config.event_name_prefix}{event.name}"

        image_data: Optional[bytes] = None
        if event.cover_image:
            try:
                image_data = await event.cover_image.read()
            except Exception as exc:
                log.warning("Could not fetch cover image for event %s: %s", event.id, exc)

        kwargs: dict = dict(
            name=name,
            description=event.description or "",
            start_time=event.start_time,
            end_time=event.end_time,
            privacy_level=discord.PrivacyLevel.guild_only,
            entity_type=event.entity_type,
        )

        if event.entity_type == discord.EntityType.external:
            kwargs["location"] = event.location or "Ver servidor principal"
        else:
            # voice or stage — try to match channel by name, else first available
            channel = None
            if event.channel:
                channel = discord.utils.get(
                    target_guild.channels, name=event.channel.name
                )
            if channel is None:
                channel = discord.utils.find(
                    lambda c: isinstance(c, (discord.VoiceChannel, discord.StageChannel)),
                    target_guild.channels,
                )
            if channel is None:
                log.warning(
                    "No voice/stage channel in guild %s; falling back to external",
                    target_guild.id,
                )
                kwargs["entity_type"] = discord.EntityType.external
                kwargs["location"] = (
                    f"#{event.channel.name}" if event.channel else "Watch primary server"
                )
            else:
                kwargs["channel"] = channel

        if image_data:
            kwargs["image"] = image_data

        try:
            relay = await target_guild.create_scheduled_event(**kwargs)
            log.info(
                "Created relay %s in guild %s for master event %s",
                relay.id, target_guild.id, event.id,
            )
            return relay
        except discord.Forbidden:
            log.error("Missing Manage Events permission in guild %s", target_guild.id)
        except discord.HTTPException as exc:
            log.error("HTTP error creating event in guild %s: %s", target_guild.id, exc)
        return None

    # ── Startup sync ─────────────────────────────────────────────────────────

    async def startup_sync(self) -> None:
        """Create missing relay entries for events that appeared while bot was offline."""
        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)
        if not master_guild:
            log.warning("Master guild not found during startup sync")
            return

        try:
            events = await master_guild.fetch_scheduled_events()
        except discord.HTTPException as exc:
            log.error("Could not fetch master guild events on startup: %s", exc)
            return

        created = 0
        for event in events:
            if event.status in (
                discord.EventStatus.completed,
                discord.EventStatus.cancelled,
            ):
                continue

            for target_cfg in self.bot.config.target_guilds:
                existing = await self.bot.db.get_relay_event_id(
                    event.id, target_cfg.guild_id
                )
                if existing:
                    continue

                target_guild = self.bot.get_guild(target_cfg.guild_id)
                if not target_guild:
                    log.warning("Target guild %s not found", target_cfg.guild_id)
                    continue

                relay = await self.create_relay_event(target_guild, event)
                if relay:
                    await self.bot.db.add_relay(event.id, target_guild.id, relay.id)
                    created += 1

                await asyncio.sleep(0.5)

        if created:
            log.info("Startup sync created %d missing relay(s)", created)
        else:
            log.info("Startup sync: all events already relayed")

    # ── Listeners ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_scheduled_event_create(
        self, event: discord.ScheduledEvent
    ) -> None:
        if not self.is_master_event(event):
            return

        log.info("New master event: %s (%s)", event.name, event.id)

        for target_cfg in self.bot.config.target_guilds:
            target_guild = self.bot.get_guild(target_cfg.guild_id)
            if not target_guild:
                log.warning("Target guild %s not available", target_cfg.guild_id)
                continue

            relay = await self.create_relay_event(target_guild, event)
            if relay:
                await self.bot.db.add_relay(event.id, target_guild.id, relay.id)

            await asyncio.sleep(0.5)

    @commands.Cog.listener()
    async def on_scheduled_event_update(
        self,
        before: discord.ScheduledEvent,
        after: discord.ScheduledEvent,
    ) -> None:
        if not self.is_master_event(after):
            return
        if not _is_meaningful_update(before, after):
            return

        log.info("Master event updated: %s (%s)", after.name, after.id)

        image_data: Optional[bytes] = None
        if after.cover_image:
            try:
                image_data = await after.cover_image.read()
            except Exception as exc:
                log.warning("Could not fetch cover image: %s", exc)

        relays = await self.bot.db.get_relays_for_master(after.id)

        for row in relays:
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue

            try:
                relay_event = await guild.fetch_scheduled_event(
                    int(row["relay_event_id"])
                )
            except discord.NotFound:
                log.warning(
                    "Relay event %s gone from guild %s; removing from DB",
                    row["relay_event_id"], guild.id,
                )
                await self.bot.db.delete_relay(after.id, guild.id)
                continue

            edit_kwargs: dict = dict(
                name=f"{self.bot.config.event_name_prefix}{after.name}",
                description=after.description or "",
                start_time=after.start_time,
                end_time=after.end_time,
            )
            if after.entity_type == discord.EntityType.external:
                edit_kwargs["location"] = after.location or "Watch primary server"
            if image_data:
                edit_kwargs["image"] = image_data

            try:
                await relay_event.edit(**edit_kwargs)
                log.info("Updated relay %s in guild %s", relay_event.id, guild.id)
            except discord.Forbidden:
                log.error("Missing permissions to edit event in guild %s", guild.id)
            except discord.HTTPException as exc:
                log.error("HTTP error editing event in guild %s: %s", guild.id, exc)

            await asyncio.sleep(0.5)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(
        self, event: discord.ScheduledEvent
    ) -> None:
        if not self.is_master_event(event):
            return

        log.info("Master event deleted: %s (%s)", event.name, event.id)

        relays = await self.bot.db.get_relays_for_master(event.id)

        for row in relays:
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue

            try:
                relay_event = await guild.fetch_scheduled_event(
                    int(row["relay_event_id"])
                )
                await relay_event.delete()
                log.info("Deleted relay %s in guild %s", row["relay_event_id"], guild.id)
            except discord.NotFound:
                log.warning(
                    "Relay event %s already gone in guild %s",
                    row["relay_event_id"], guild.id,
                )
            except discord.Forbidden:
                log.error("Missing permissions to delete event in guild %s", guild.id)
            except discord.HTTPException as exc:
                log.error("HTTP error deleting event in guild %s: %s", guild.id, exc)

            await asyncio.sleep(0.5)

        await self.bot.db.delete_relays_for_master(event.id)


async def setup(bot) -> None:
    await bot.add_cog(RelayEvents(bot))
