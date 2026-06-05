import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

_WINDOW_MINUTES = 1  # check every minute
_TOLERANCE = 1       # ± tolerance in minutes for the trigger window


class Reminders(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.reminder_task.start()

    def _relay_display_name(
        self,
        target_cfg,
        event: discord.ScheduledEvent,
        relay_event: discord.ScheduledEvent | None,
    ) -> str:
        if "name" in target_cfg.relay_field_set():
            return relay_event.name if relay_event is not None else event.name

        host_name = None
        creator = getattr(event, "creator", None)
        if creator and getattr(creator, "name", None):
            host_name = creator.name
        elif getattr(event, "creator_id", None) is not None and event.guild is not None:
            member = event.guild.get_member(event.creator_id)
            if member and getattr(member, "name", None):
                host_name = member.name

        if host_name:
            return f"[PD] - Help Needed - {host_name}"

        if relay_event is not None:
            return relay_event.name

        return event.name

    def _event_host_name(self, event: discord.ScheduledEvent) -> str | None:
        creator = getattr(event, "creator", None)
        if creator and getattr(creator, "name", None):
            return creator.name

        creator_id = getattr(event, "creator_id", None)
        if creator_id is None or event.guild is None:
            return None

        member = event.guild.get_member(creator_id)
        if member and getattr(member, "name", None):
            return member.name

        user = event.guild._state.get_user(creator_id)
        if user and getattr(user, "name", None):
            return user.name

        return None

    def _log_channel_permissions(
        self,
        channel: discord.abc.GuildChannel,
    ) -> str:
        me = channel.guild.me
        if me is None:
            return "bot member not cached"

        perms = channel.permissions_for(me)
        return (
            f"view_channel={perms.view_channel}, "
            f"send_messages={perms.send_messages}, "
            f"embed_links={perms.embed_links}, "
            f"attach_files={perms.attach_files}"
        )

    def cog_unload(self) -> None:
        self.reminder_task.cancel()

    @tasks.loop(minutes=_WINDOW_MINUTES)
    async def reminder_task(self) -> None:
        now = datetime.now(timezone.utc)
        reminder_minutes = self.bot.config.reminder_minutes_before
        window_start = now + timedelta(minutes=reminder_minutes - _TOLERANCE)
        window_end = now + timedelta(minutes=reminder_minutes + _TOLERANCE)

        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)
        if not master_guild:
            return

        unreminded_relays = await self.bot.db.get_unreminded_relays()
        grouped: dict[int, list[dict]] = {}
        for row in unreminded_relays:
            grouped.setdefault(int(row["master_event_id"]), []).append(row)

        for master_event_id, relay_rows in grouped.items():
            try:
                event = await master_guild.fetch_scheduled_event(master_event_id)
            except discord.NotFound:
                # Event gone - mark so we never check it again.
                await self.bot.db.mark_all_reminded(master_event_id)
                continue
            except discord.HTTPException as exc:
                log.warning("Could not fetch event %s: %s", master_event_id, exc)
                continue

            if not (window_start <= event.start_time <= window_end):
                continue

            await self._send_reminders(event, master_event_id, relay_rows)

    async def _send_reminders(
        self,
        event: discord.ScheduledEvent,
        master_event_id: int,
        relay_rows: list[dict] | None = None,
        mark_sent: bool = True,
    ) -> None:
        relays = relay_rows or await self.bot.db.get_relays_for_master(master_event_id)

        for row in relays:
            target_cfg = self.bot.config.get_target_guild(int(row["guild_id"]))
            if not target_cfg or not target_cfg.reminder_channel_id:
                if mark_sent:
                    await self.bot.db.mark_reminded(master_event_id, int(row["guild_id"]))
                continue

            channel = self.bot.get_channel(target_cfg.reminder_channel_id)
            if not channel or not isinstance(
                channel, (discord.TextChannel, discord.Thread)
            ):
                log.warning(
                    "Reminder channel %s not found or not a text channel",
                    target_cfg.reminder_channel_id,
                )
                continue

            mentions: list[str] = []
            relay_event = None
            try:
                relay_event = await channel.guild.fetch_scheduled_event(
                    int(row["relay_event_id"])
                )
                async for user in relay_event.users():
                    mentions.append(user.mention)
            except discord.NotFound:
                log.warning(
                    "Relay event %s not found in guild %s while building reminder mentions",
                    row["relay_event_id"],
                    channel.guild.id,
                )
            except Exception as exc:
                log.warning(
                    "Could not fetch participants for relay event %s in guild %s: %s",
                    row["relay_event_id"],
                    channel.guild.id,
                    exc,
                )

            mentions_str = " ".join(mentions)

            display_event_name = self._relay_display_name(target_cfg, event, relay_event)

            content = self.bot.config.reminder_message.format(
                event_name=display_event_name,
                mentions=mentions_str,
                minutes=self.bot.config.reminder_minutes_before,
            ).strip()

            # Build embed (master event primary)
            embed = discord.Embed(
                title=display_event_name,
                color=discord.Color.orange(),
                timestamp=event.start_time,
            )

            # Add only relay/server-specific fields according to target guild's relay_fields
            allowed = target_cfg.relay_field_set()
            if relay_event is not None:
                # Show relay event name if allowed
                if "name" in allowed and getattr(relay_event, "name", None):
                    embed.add_field(
                        name="Event",
                        value=relay_event.name,
                        inline=False,
                    )

                # Relay event location
                if "location" in allowed and getattr(relay_event, "location", None):
                    embed.add_field(
                        name="Location",
                        value=str(relay_event.location),
                        inline=True,
                    )

                # Relay event end time
                if "end_time" in allowed and getattr(relay_event, "end_time", None):
                    embed.add_field(
                        name="Ends",
                        value=discord.utils.format_dt(relay_event.end_time, style="F"),
                        inline=True,
                    )

                # Relay event description
                if "description" in allowed and getattr(relay_event, "description", None):
                    # Truncate to 1000 chars to stay within embed limits
                    desc = str(relay_event.description)[:1000]
                    embed.add_field(
                        name="Description",
                        value=desc,
                        inline=False,
                    )

                # Relay event image as thumbnail
                if "image" in allowed and getattr(relay_event, "cover_image", None):
                    try:
                        embed.set_thumbnail(url=relay_event.cover_image.url)
                    except Exception:
                        pass

            if "name" not in allowed:
                host_name = self._event_host_name(event)
                if host_name:
                    embed.add_field(name="Host", value=host_name, inline=True)

            try:
                await channel.send(
                    content=content or None,
                    embed=embed,
                )
                if mark_sent:
                    await self.bot.db.mark_reminded(master_event_id, int(row["guild_id"]))
                await self.bot.db.log_audit(
                    "reminder_sent",
                    master_event_id=master_event_id,
                    guild_id=int(row["guild_id"]),
                    relay_event_id=int(row["relay_event_id"]),
                    details=f"sent reminder to channel {channel.id}",
                )
                log.info(
                    "Sent reminder for event %s to guild %s",
                    master_event_id, row["guild_id"],
                )
            except discord.Forbidden as exc:
                log.error(
                    "Missing permissions to send in channel %s (%s): %s",
                    target_cfg.reminder_channel_id,
                    self._log_channel_permissions(channel),
                    exc,
                )
                await self.bot.db.log_audit(
                    "reminder_failed",
                    master_event_id=master_event_id,
                    guild_id=int(row["guild_id"]),
                    relay_event_id=int(row["relay_event_id"]),
                    details=(
                        f"forbidden while sending reminder to channel {channel.id}; "
                        f"channel_perms={self._log_channel_permissions(channel)}"
                    ),
                )
            except discord.HTTPException as exc:
                log.error("HTTP error sending reminder: %s", exc)
                await self.bot.db.log_audit(
                    "reminder_failed",
                    master_event_id=master_event_id,
                    guild_id=int(row["guild_id"]),
                    relay_event_id=int(row["relay_event_id"]),
                    details=f"http error while sending reminder: {exc}",
                )

    @reminder_task.before_loop
    async def before_reminder(self) -> None:
        await self.bot.wait_until_ready()

    @reminder_task.error
    async def reminder_error(self, exc: Exception) -> None:
        log.exception("Unhandled error in reminder task: %s", exc)


async def setup(bot) -> None:
    await bot.add_cog(Reminders(bot))
