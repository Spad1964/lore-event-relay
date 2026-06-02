import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

AUTO_REPAIR_MINUTES = 30


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
        name = f"{self.bot.config.event_name_prefix}{event.name}"

        image_data: Optional[bytes] = None
        if event.cover_image:
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
            description=event.description or "",
            start_time=event.start_time,
            end_time=event.end_time,
            privacy_level=discord.PrivacyLevel.guild_only,
            entity_type=event.entity_type,
        )

        if event.entity_type == discord.EntityType.external:
            kwargs["location"] = event.location or "Ver servidor principal"
        else:
            channel = self._find_matching_event_channel(target_guild, event)
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
                if not self._channel_is_usable(target_guild, channel):
                    log.warning(
                        "Matched channel %s in guild %s is not usable (%s); falling back to external",
                        channel.id,
                        target_guild.id,
                        self._log_channel_permissions(target_guild, channel),
                    )
                    kwargs["entity_type"] = discord.EntityType.external
                    kwargs["location"] = (
                        f"#{event.channel.name}" if event.channel else "Watch primary server"
                    )
                else:
                    log.info(
                        "Using relay channel %s (%s) in guild %s",
                        channel.name,
                        channel.id,
                        target_guild.id,
                    )
                    kwargs["channel"] = channel

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

            cover_removed = before.cover_image is not None and after.cover_image is None
            if cover_removed or relay_event.entity_type != after.entity_type:
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
                name=f"{self.bot.config.event_name_prefix}{after.name}",
                description=after.description or "",
                start_time=after.start_time,
                end_time=after.end_time,
            )
            if after.entity_type == discord.EntityType.external:
                edit_kwargs["location"] = after.location or "Watch primary server"
            else:
                channel = self._find_matching_event_channel(guild, after)
                if channel:
                    edit_kwargs["channel"] = channel
            if image_data:
                edit_kwargs["image"] = image_data

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