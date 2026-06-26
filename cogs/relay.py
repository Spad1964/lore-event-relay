import asyncio
import logging
from datetime import timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks

from config import DEFAULT_RELAY_FIELDS

log = logging.getLogger(__name__)

AUTO_REPAIR_MINUTES = 30
_RELAY_LOCATION_FALLBACK = "See the master server"
_RELAY_NAME_FALLBACK = "[PD] - Help Needed"
# External scheduled events require an end_time. When the master event lacks
# one, fall back to this duration after the start time.
_RELAY_DEFAULT_DURATION = timedelta(hours=1)


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
        or (
            before.cover_image
            and after.cover_image
            and before.cover_image.url != after.cover_image.url
        )
        or (before.cover_image is None) != (after.cover_image is None)
    )


class RelayEvents(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.auto_repair_task.start()

    def cog_unload(self) -> None:
        self.auto_repair_task.cancel()

    def _log_guild_permissions(self, guild: discord.Guild) -> str:
        me = guild.me
        if me is None:
            return "bot member not cached"

        perms = me.guild_permissions
        return (
            f"manage_events={perms.manage_events}, "
            f"view_channel={perms.view_channel}, "
            f"send_messages={perms.send_messages}, "
            f"connect={perms.connect}, "
            f"speak={perms.speak}"
        )

    def _log_channel_permissions(
        self,
        guild: discord.Guild,
        channel: discord.abc.GuildChannel,
    ) -> str:
        me = guild.me
        if me is None:
            return "bot member not cached"

        perms = channel.permissions_for(me)
        return (
            f"view_channel={perms.view_channel}, "
            f"connect={perms.connect}, "
            f"speak={perms.speak}, "
            f"manage_channels={perms.manage_channels}"
        )

    def is_master_event(self, event: discord.ScheduledEvent) -> bool:
        return event.guild_id == self.bot.config.master_guild_id

    def _find_matching_event_channel(
        self,
        target_guild: discord.Guild,
        event: discord.ScheduledEvent,
    ) -> Optional[discord.abc.GuildChannel]:
        if event.channel:
            channel = discord.utils.get(
                target_guild.channels,
                name=event.channel.name,
                type=event.channel.type,
            )
            if channel:
                return channel

        return discord.utils.find(
            lambda c: isinstance(c, (discord.VoiceChannel, discord.StageChannel)),
            target_guild.channels,
        )

    def _channel_is_usable(
        self,
        guild: discord.Guild,
        channel: discord.abc.GuildChannel,
    ) -> bool:
        me = guild.me
        if me is None:
            return False

        perms = channel.permissions_for(me)
        return perms.view_channel and perms.connect

    def _relay_field_set(self, guild_id: int) -> set[str]:
        target_cfg = self.bot.config.get_target_guild(guild_id)
        if target_cfg is None:
            return set(DEFAULT_RELAY_FIELDS)
        return target_cfg.relay_field_set()

    def _relay_event_name(
        self,
        target_guild_id: int,
        event: discord.ScheduledEvent,
    ) -> str:
        relay_fields = self._relay_field_set(target_guild_id)
        if "name" not in relay_fields:
            host_name = self._event_host_name(event)
            if host_name:
                return f"{_RELAY_NAME_FALLBACK} - {host_name}"
            return _RELAY_NAME_FALLBACK
        return f"{self.bot.config.event_name_prefix}{event.name}"

    def _event_host_name(self, event: discord.ScheduledEvent) -> Optional[str]:
        creator = getattr(event, "creator", None)
        if creator and getattr(creator, "name", None):
            return creator.name

        creator_id = getattr(event, "creator_id", None)
        if creator_id is None:
            return None

        guild = event.guild
        if guild is None:
            return None

        member = guild.get_member(creator_id)
        if member and getattr(member, "name", None):
            return member.name

        user = guild._state.get_user(creator_id)
        if user and getattr(user, "name", None):
            return user.name

        return None

    async def _complete_relay_event(
        self,
        guild: discord.Guild,
        master_event_id: int,
        relay_event: discord.ScheduledEvent,
        relay_row: dict,
    ) -> None:
        try:
            if relay_event.status is discord.EventStatus.active:
                await relay_event.end()
            else:
                await relay_event.edit(status=discord.EventStatus.completed)

            log.info("Ended relay %s in guild %s", relay_event.id, guild.id)
            await self.bot.db.log_audit(
                "relay_ended",
                master_event_id=master_event_id,
                guild_id=guild.id,
                relay_event_id=relay_event.id,
                details="ended mirrored relay event",
            )
        except discord.Forbidden:
            log.error(
                "Missing permissions to end event in guild %s (%s)",
                guild.id,
                self._log_guild_permissions(guild),
            )
            await self.bot.db.log_audit(
                "relay_update_failed",
                master_event_id=master_event_id,
                guild_id=guild.id,
                relay_event_id=relay_event.id,
                details="forbidden while ending mirrored relay",
            )
        except discord.HTTPException as exc:
            log.error("HTTP error ending event in guild %s: %s", guild.id, exc)
            await self.bot.db.log_audit(
                "relay_update_failed",
                master_event_id=master_event_id,
                guild_id=guild.id,
                relay_event_id=relay_event.id,
                details=f"http error while ending mirrored relay: {exc}",
            )

    async def ensure_relay_for_target(
        self,
        target_guild: discord.Guild,
        event: discord.ScheduledEvent,
        *,
        dry_run: bool = False,
    ) -> bool:
        existing_id = await self.bot.db.get_relay_event_id(event.id, target_guild.id)
        repairing = False

        if existing_id:
            try:
                await target_guild.fetch_scheduled_event(existing_id)
                return False
            except discord.NotFound:
                repairing = True
                log.warning(
                    "Relay event %s missing from guild %s; recreating",
                    existing_id,
                    target_guild.id,
                )
                if dry_run:
                    return True
                await self.bot.db.delete_relay(event.id, target_guild.id)
            except discord.HTTPException as exc:
                log.warning(
                    "Could not verify relay %s in guild %s: %s",
                    existing_id,
                    target_guild.id,
                    exc,
                )
                return False

        if dry_run:
            return True

        relay = await self.create_relay_event(target_guild, event)
        if relay:
            await self.bot.db.add_relay(event.id, target_guild.id, relay.id)
            await self.bot.db.log_audit(
                "relay_repaired" if repairing else "relay_created",
                master_event_id=event.id,
                guild_id=target_guild.id,
                relay_event_id=relay.id,
                details=(
                    "recreated missing relay"
                    if repairing
                    else "created relay"
                ),
            )
            return True

        return False

    # ── Create relay ─────────────────────────────────────────────────────────

    async def create_relay_event(
        self,
        target_guild: discord.Guild,
        event: discord.ScheduledEvent,
    ) -> Optional[discord.ScheduledEvent]:
        relay_fields = self._relay_field_set(target_guild.id)
        name = self._relay_event_name(target_guild.id, event)

        image_data: Optional[bytes] = None
        if "image" in relay_fields and event.cover_image:
            try:
                image_data = await event.cover_image.read()
            except Exception as exc:
                log.warning(
                    "Could not fetch cover image for event %s: %s",
                    event.id,
                    exc,
                )

        kwargs: dict = dict(
            name=name,
            start_time=event.start_time,
            privacy_level=discord.PrivacyLevel.guild_only,
            entity_type=event.entity_type,
        )

        if "description" in relay_fields:
            kwargs["description"] = event.description or ""

        if event.end_time is not None and (
            event.entity_type == discord.EntityType.external
            or "end_time" in relay_fields
        ):
            kwargs["end_time"] = event.end_time

        if event.entity_type == discord.EntityType.external:
            kwargs["location"] = (
                event.location if "location" in relay_fields and event.location
                else _RELAY_LOCATION_FALLBACK
            )
        else:
            channel = self._find_matching_event_channel(target_guild, event)
            if channel is None:
                log.warning(
                    "No voice/stage channel in guild %s; falling back to external",
                    target_guild.id,
                )
                kwargs["entity_type"] = discord.EntityType.external
                kwargs["location"] = _RELAY_LOCATION_FALLBACK
            else:
                if not self._channel_is_usable(target_guild, channel):
                    log.warning(
                        "Matched channel %s in guild %s is not usable (%s); falling back to external",
                        channel.id,
                        target_guild.id,
                        self._log_channel_permissions(target_guild, channel),
                    )
                    kwargs["entity_type"] = discord.EntityType.external
                    kwargs["location"] = _RELAY_LOCATION_FALLBACK
                else:
                    log.info(
                        "Using relay channel %s (%s) in guild %s",
                        channel.name,
                        channel.id,
                        target_guild.id,
                    )
                    kwargs["channel"] = channel

        # External events (including channel-based events that fell back to
        # external above) must have an end_time; supply one if missing.
        if (
            kwargs["entity_type"] == discord.EntityType.external
            and "end_time" not in kwargs
        ):
            kwargs["end_time"] = event.start_time + _RELAY_DEFAULT_DURATION

        if image_data:
            kwargs["image"] = image_data

        try:
            relay = await target_guild.create_scheduled_event(**kwargs)
            log.info(
                "Created relay %s in guild %s for master event %s",
                relay.id,
                target_guild.id,
                event.id,
            )
            return relay
        except discord.Forbidden as exc:
            log.error(
                "Forbidden creating relay in guild %s (%s): %s",
                target_guild.id,
                self._log_guild_permissions(target_guild),
                exc,
            )
            await self.bot.db.log_audit(
                "relay_create_failed",
                master_event_id=event.id,
                guild_id=target_guild.id,
                details=(
                    f"forbidden while creating {event.entity_type.name} relay; "
                    f"guild_perms={self._log_guild_permissions(target_guild)}"
                ),
            )
        except discord.HTTPException as exc:
            log.error(
                "HTTP error creating event in guild %s: %s",
                target_guild.id,
                exc,
            )
            await self.bot.db.log_audit(
                "relay_create_failed",
                master_event_id=event.id,
                guild_id=target_guild.id,
                details=f"http error while creating relay: {exc}",
            )
        return None

    # ── Startup sync ─────────────────────────────────────────────────────────

    async def startup_sync(self) -> None:
        """Create missing relay entries for events that appeared while bot was offline."""
        created = await self.sync_missing_relays()
        if created:
            log.info("Startup sync created %d missing relay(s)", created)
        else:
            log.info("Startup sync: all events already relayed")

    async def sync_missing_relays(self, *, dry_run: bool = False) -> int:
        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)
        if not master_guild:
            log.warning("Master guild not found during startup sync")
            return 0

        try:
            events = await master_guild.fetch_scheduled_events()
        except discord.HTTPException as exc:
            log.error("Could not fetch master guild events on startup: %s", exc)
            return 0

        created = 0
        for event in events:
            if event.status in (
                discord.EventStatus.completed,
                discord.EventStatus.cancelled,
            ):
                continue

            for target_cfg in self.bot.config.target_guilds:
                target_guild = self.bot.get_guild(target_cfg.guild_id)
                if not target_guild:
                    log.warning("Target guild %s not found", target_cfg.guild_id)
                    continue

                existing_id = await self.bot.db.get_relay_event_id(
                    event.id, target_guild.id
                )
                if existing_id:
                    continue

                if dry_run:
                    created += 1
                    continue

                created_relay = await self.ensure_relay_for_target(
                    target_guild,
                    event,
                )
                if created_relay:
                    created += 1

                await asyncio.sleep(0.5)

        if dry_run:
            log.info("Dry-run sync would create %d relay(s)", created)
        else:
            log.info("Sync created %d missing relay(s)", created)
        return created

    async def repair_missing_relays(self, *, dry_run: bool = False) -> int:
        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)
        if not master_guild:
            log.warning("Master guild not found during repair pass")
            return 0

        try:
            events = await master_guild.fetch_scheduled_events()
        except discord.HTTPException as exc:
            log.error("Could not fetch master guild events during repair: %s", exc)
            return 0

        repaired = 0
        for event in events:
            if event.status in (
                discord.EventStatus.completed,
                discord.EventStatus.cancelled,
            ):
                continue

            for target_cfg in self.bot.config.target_guilds:
                target_guild = self.bot.get_guild(target_cfg.guild_id)
                if not target_guild:
                    log.warning("Target guild %s not found", target_cfg.guild_id)
                    continue

                if await self.ensure_relay_for_target(
                    target_guild,
                    event,
                    dry_run=dry_run,
                ):
                    repaired += 1

                await asyncio.sleep(0.5)

        if dry_run:
            log.info("Dry-run repair would recreate %d relay(s)", repaired)
        else:
            log.info("Repair recreated %d missing relay(s)", repaired)
        return repaired

    @tasks.loop(minutes=AUTO_REPAIR_MINUTES)
    async def auto_repair_task(self) -> None:
        await self.repair_missing_relays()

    @auto_repair_task.before_loop
    async def before_auto_repair(self) -> None:
        await self.bot.wait_until_ready()

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

            await self.ensure_relay_for_target(target_guild, event)

            await asyncio.sleep(0.5)

    @commands.Cog.listener()
    async def on_scheduled_event_update(
        self,
        before: discord.ScheduledEvent,
        after: discord.ScheduledEvent,
    ) -> None:
        if not self.is_master_event(after):
            return
        if after.status is discord.EventStatus.completed and before.status is not discord.EventStatus.completed:
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
                        row["relay_event_id"],
                        guild.id,
                    )
                    await self.bot.db.delete_relay(after.id, guild.id)
                    await self.bot.db.log_audit(
                        "relay_missing",
                        master_event_id=after.id,
                        guild_id=guild.id,
                        relay_event_id=int(row["relay_event_id"]),
                        details="relay disappeared before completion could be applied",
                    )
                    continue

                await self._complete_relay_event(guild, after.id, relay_event, row)
                await asyncio.sleep(0.5)
            return

        if not _is_meaningful_update(before, after):
            return

        log.info("Master event updated: %s (%s)", after.name, after.id)

        image_data: Optional[bytes] = None
        image_fetch_failed = False
        if after.cover_image:
            try:
                image_data = await after.cover_image.read()
            except Exception as exc:
                log.warning("Could not fetch cover image: %s", exc)
                image_fetch_failed = True

        relays = await self.bot.db.get_relays_for_master(after.id)

        for row in relays:
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue

            relay_fields = self._relay_field_set(guild.id)

            try:
                relay_event = await guild.fetch_scheduled_event(
                    int(row["relay_event_id"])
                )
            except discord.NotFound:
                log.warning(
                    "Relay event %s gone from guild %s; removing from DB",
                    row["relay_event_id"],
                    guild.id,
                )
                await self.bot.db.delete_relay(after.id, guild.id)
                await self.bot.db.log_audit(
                    "relay_missing",
                    master_event_id=after.id,
                    guild_id=guild.id,
                    relay_event_id=int(row["relay_event_id"]),
                    details="relay disappeared before update could be applied",
                )
                continue

            if relay_event.entity_type != after.entity_type:
                try:
                    await relay_event.delete()
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    log.error(
                        "Missing permissions to recreate event in guild %s (%s)",
                        guild.id,
                        self._log_guild_permissions(guild),
                    )
                    await self.bot.db.log_audit(
                        "relay_update_failed",
                        master_event_id=after.id,
                        guild_id=guild.id,
                        relay_event_id=relay_event.id,
                        details="forbidden while deleting relay before recreation",
                    )
                    continue
                except discord.HTTPException as exc:
                    log.error(
                        "HTTP error recreating event in guild %s: %s",
                        guild.id,
                        exc,
                    )
                    await self.bot.db.log_audit(
                        "relay_update_failed",
                        master_event_id=after.id,
                        guild_id=guild.id,
                        relay_event_id=relay_event.id,
                        details=f"http error while deleting relay before recreation: {exc}",
                    )
                    continue

                await self.bot.db.delete_relay(after.id, guild.id)
                await self.ensure_relay_for_target(guild, after)
                await asyncio.sleep(0.5)
                continue

            edit_kwargs: dict = dict(
                name=self._relay_event_name(guild.id, after),
                start_time=after.start_time,
            )
            if "description" in relay_fields:
                edit_kwargs["description"] = after.description or ""
            else:
                edit_kwargs["description"] = ""

            if after.entity_type == discord.EntityType.external:
                edit_kwargs["location"] = (
                    after.location if "location" in relay_fields and after.location
                    else _RELAY_LOCATION_FALLBACK
                )
                edit_kwargs["end_time"] = (
                    after.end_time
                    or after.start_time + _RELAY_DEFAULT_DURATION
                )
            else:
                edit_kwargs["end_time"] = after.end_time if "end_time" in relay_fields else None
                channel = self._find_matching_event_channel(guild, after)
                if channel:
                    edit_kwargs["channel"] = channel

            if "image" in relay_fields:
                if image_fetch_failed:
                    pass
                else:
                    edit_kwargs["image"] = (
                        image_data if image_data is not None else None
                    )
            else:
                edit_kwargs["image"] = None

            try:
                await relay_event.edit(**edit_kwargs)
                log.info("Updated relay %s in guild %s", relay_event.id, guild.id)
                await self.bot.db.log_audit(
                    "relay_updated",
                    master_event_id=after.id,
                    guild_id=guild.id,
                    relay_event_id=relay_event.id,
                    details="updated mirrored relay event",
                )
            except discord.Forbidden:
                log.error(
                    "Missing permissions to edit event in guild %s (%s)",
                    guild.id,
                    self._log_guild_permissions(guild),
                )
                await self.bot.db.log_audit(
                    "relay_update_failed",
                    master_event_id=after.id,
                    guild_id=guild.id,
                    relay_event_id=relay_event.id,
                    details="forbidden while editing mirrored relay",
                )
            except discord.HTTPException as exc:
                log.error("HTTP error editing event in guild %s: %s", guild.id, exc)
                await self.bot.db.log_audit(
                    "relay_update_failed",
                    master_event_id=after.id,
                    guild_id=guild.id,
                    relay_event_id=relay_event.id,
                    details=f"http error while editing mirrored relay: {exc}",
                )

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
                await self.bot.db.log_audit(
                    "relay_deleted",
                    master_event_id=event.id,
                    guild_id=guild.id,
                    relay_event_id=int(row["relay_event_id"]),
                    details="deleted mirrored relay event",
                )
            except discord.NotFound:
                log.warning(
                    "Relay event %s already gone in guild %s",
                    row["relay_event_id"],
                    guild.id,
                )
                await self.bot.db.log_audit(
                    "relay_missing",
                    master_event_id=event.id,
                    guild_id=guild.id,
                    relay_event_id=int(row["relay_event_id"]),
                    details="relay already gone before delete could be applied",
                )
            except discord.Forbidden:
                log.error(
                    "Missing permissions to delete event in guild %s (%s)",
                    guild.id,
                    self._log_guild_permissions(guild),
                )
                await self.bot.db.log_audit(
                    "relay_delete_failed",
                    master_event_id=event.id,
                    guild_id=guild.id,
                    relay_event_id=int(row["relay_event_id"]),
                    details="forbidden while deleting mirrored relay",
                )
            except discord.HTTPException as exc:
                log.error("HTTP error deleting event in guild %s: %s", guild.id, exc)
                await self.bot.db.log_audit(
                    "relay_delete_failed",
                    master_event_id=event.id,
                    guild_id=guild.id,
                    relay_event_id=int(row["relay_event_id"]),
                    details=f"http error while deleting mirrored relay: {exc}",
                )

            await asyncio.sleep(0.5)

        await self.bot.db.delete_relays_for_master(event.id)


async def setup(bot) -> None:
    await bot.add_cog(RelayEvents(bot))