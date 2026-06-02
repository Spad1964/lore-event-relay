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

            # Collect @mentions from master event subscribers
            mentions: list[str] = []
            try:
                async for user in event.users():
                    mentions.append(user.mention)
            except Exception as exc:
                log.warning(
                    "Could not fetch participants for event %s: %s",
                    master_event_id, exc,
                )

            mentions_str = " ".join(mentions)

            await self._send_reminders(event, master_event_id, mentions_str, relay_rows)

    async def _send_reminders(
        self,
        event: discord.ScheduledEvent,
        master_event_id: int,
        mentions_str: str,
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

            content = self.bot.config.reminder_message.format(
                event_name=event.name,
                mentions=mentions_str,
                minutes=self.bot.config.reminder_minutes_before,
            ).strip()

            # Build embed
            embed = discord.Embed(
                title=event.name,
                description=event.description or "",
                color=discord.Color.orange(),
                timestamp=event.start_time,
            )
            embed.set_footer(
                text=f"Starts in {self.bot.config.reminder_minutes_before} minutes"
            )

            # Link to the relay event in that guild
            try:
                relay_event = await channel.guild.fetch_scheduled_event(
                    int(row["relay_event_id"])
                )
                embed.url = (
                    f"https://discord.com/events/{channel.guild.id}/{relay_event.id}"
                )
            except discord.NotFound:
                pass

            # Cover image from master event
            if event.cover_image:
                embed.set_image(url=event.cover_image.url)

            embed.add_field(
                name="Início",
                value=discord.utils.format_dt(event.start_time, style="F"),
                inline=True,
            )
            if event.end_time:
                embed.add_field(
                    name="Fim",
                    value=discord.utils.format_dt(event.end_time, style="F"),
                    inline=True,
                )

            try:
                await channel.send(
                    content=content or None,
                    embed=embed,
                )
                if mark_sent:
                    await self.bot.db.mark_reminded(master_event_id, int(row["guild_id"]))
                log.info(
                    "Sent reminder for event %s to guild %s",
                    master_event_id, row["guild_id"],
                )
            except discord.Forbidden:
                log.error(
                    "Missing permissions to send in channel %s",
                    target_cfg.reminder_channel_id,
                )
            except discord.HTTPException as exc:
                log.error("HTTP error sending reminder: %s", exc)

    @reminder_task.before_loop
    async def before_reminder(self) -> None:
        await self.bot.wait_until_ready()

    @reminder_task.error
    async def reminder_error(self, exc: Exception) -> None:
        log.exception("Unhandled error in reminder task: %s", exc)


async def setup(bot) -> None:
    await bot.add_cog(Reminders(bot))
