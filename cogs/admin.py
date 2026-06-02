import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


class Admin(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    relay = app_commands.Group(
        name="relay",
        description="Lore Event Relay management",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ── /relay info ──────────────────────────────────────────────────────────

    @relay.command(name="info", description="Show bot config and guild status")
    async def info(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Lore Event Relay — Info",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)
        embed.add_field(
            name="Master Guild",
            value=(
                f"**{master_guild.name}** (`{self.bot.config.master_guild_id}`)"
                if master_guild
                else f"Not found (`{self.bot.config.master_guild_id}`)"
            ),
            inline=False,
        )

        lines = []
        for t in self.bot.config.target_guilds:
            g = self.bot.get_guild(t.guild_id)
            name = f"**{g.name}**" if g else "❌ Not found"
            ch = f"<#{t.reminder_channel_id}>" if t.reminder_channel_id else "—"
            lines.append(f"• {name} (`{t.guild_id}`) — reminder: {ch}")

        embed.add_field(
            name=f"Target Guilds ({len(self.bot.config.target_guilds)})",
            value="\n".join(lines) if lines else "None configured",
            inline=False,
        )

        all_relays = await self.bot.db.get_all_relays()
        embed.add_field(name="Relays on DB", value=str(len(all_relays)), inline=True)
        embed.add_field(
            name="Prefix",
            value=f"`{self.bot.config.event_name_prefix}`",
            inline=True,
        )
        embed.add_field(
            name="Reminder",
            value=f"{self.bot.config.reminder_minutes_before} minutes before",
            inline=True,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /relay status ────────────────────────────────────────────────────────

    @relay.command(
        name="status",
        description="Show active relay mappings (event → guilds)",
    )
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        relays = await self.bot.db.get_all_relays()
        if not relays:
            await interaction.followup.send("No relays on DB.", ephemeral=True)
            return

        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)

        # Group by master_event_id
        grouped: dict[str, list[dict]] = {}
        for row in relays:
            grouped.setdefault(row["master_event_id"], []).append(row)

        embed = discord.Embed(
            title="Relay Status",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        for master_event_id, rows in grouped.items():
            event_label = f"Event `{master_event_id}`"
            if master_guild:
                try:
                    ev = await master_guild.fetch_scheduled_event(int(master_event_id))
                    event_label = ev.name
                except discord.NotFound:
                    event_label = f"~~{master_event_id}~~ (apagado)"

            target_lines = []
            for r in rows:
                reminded = "reminded" if r["reminded"] else "pending"
                target_lines.append(
                    f"• Guild `{r['guild_id']}` → relay `{r['relay_event_id']}` {reminded}"
                )

            embed.add_field(
                name=event_label,
                value="\n".join(target_lines),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /relay sync ──────────────────────────────────────────────────────────

    @relay.command(
        name="sync",
        description="Create missing relays for existing master events",
    )
    async def sync_events(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)
        if not master_guild:
            await interaction.followup.send(
                "Master guild not found.", ephemeral=True
            )
            return

        relay_cog = self.bot.get_cog("RelayEvents")
        if not relay_cog:
            await interaction.followup.send(
                "Relay cog not loaded.", ephemeral=True
            )
            return

        try:
            events = await master_guild.fetch_scheduled_events()
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Error getting events: {exc}", ephemeral=True
            )
            return

        created = skipped = 0

        for event in events:
            if event.status in (
                discord.EventStatus.completed,
                discord.EventStatus.cancelled,
            ):
                skipped += 1
                continue

            for target_cfg in self.bot.config.target_guilds:
                existing = await self.bot.db.get_relay_event_id(
                    event.id, target_cfg.guild_id
                )
                if existing:
                    skipped += 1
                    continue

                target_guild = self.bot.get_guild(target_cfg.guild_id)
                if not target_guild:
                    skipped += 1
                    continue

                created_relay = await relay_cog.ensure_relay_for_target(
                    target_guild,
                    event,
                )
                if created_relay:
                    created += 1
                else:
                    skipped += 1

        await interaction.followup.send(
            f"Sync finished: **{created}** created, **{skipped}** ignored.",
            ephemeral=True,
        )

    # ── /relay repair ────────────────────────────────────────────────────────

    @relay.command(
        name="repair",
        description="Verify relay events and recreate missing copies",
    )
    async def repair(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)
        if not master_guild:
            await interaction.followup.send(
                "Master guild not found.", ephemeral=True
            )
            return

        relay_cog = self.bot.get_cog("RelayEvents")
        if not relay_cog:
            await interaction.followup.send(
                "Relay cog not loaded.", ephemeral=True
            )
            return

        try:
            events = await master_guild.fetch_scheduled_events()
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Error getting events: {exc}", ephemeral=True
            )
            return

        active_events = [
            event
            for event in events
            if event.status
            not in (discord.EventStatus.completed, discord.EventStatus.cancelled)
        ]

        repaired = skipped = 0
        for event in active_events:
            for target_cfg in self.bot.config.target_guilds:
                target_guild = self.bot.get_guild(target_cfg.guild_id)
                if not target_guild:
                    skipped += 1
                    continue

                created_relay = await relay_cog.ensure_relay_for_target(
                    target_guild,
                    event,
                )
                if created_relay:
                    repaired += 1
                else:
                    skipped += 1

        await interaction.followup.send(
            f"Repair finished: **{repaired}** recreated, **{skipped}** already OK or skipped.",
            ephemeral=True,
        )

    # ── /relay cleanup ───────────────────────────────────────────────────────

    @relay.command(
        name="cleanup",
        description="Remove DB entries for events that no longer exist",
    )
    async def cleanup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)
        if not master_guild:
            await interaction.followup.send(
                "Master guild not found.", ephemeral=True
            )
            return

        relays = await self.bot.db.get_all_relays()
        checked: set[int] = set()
        removed = 0

        for row in relays:
            mid = int(row["master_event_id"])
            if mid in checked:
                continue
            checked.add(mid)

            try:
                await master_guild.fetch_scheduled_event(mid)
            except discord.NotFound:
                await self.bot.db.delete_relays_for_master(mid)
                removed += 1

        await interaction.followup.send(
            f"Cleanup: removed entries, **{removed}** event(s) delete(d).",
            ephemeral=True,
        )

    # ── /relay remind_test ───────────────────────────────────────────────────

    @relay.command(
        name="remind_test",
        description="Force-send the 30-min reminder for a specific master event ID",
    )
    @app_commands.describe(master_event_id="Event ID on master server")
    async def remind_test(
        self, interaction: discord.Interaction, master_event_id: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)
        if not master_guild:
            await interaction.followup.send(
                "Master guild not found.", ephemeral=True
            )
            return

        try:
            event = await master_guild.fetch_scheduled_event(int(master_event_id))
        except (discord.NotFound, ValueError):
            await interaction.followup.send(
                "Event not found.", ephemeral=True
            )
            return

        reminder_cog = self.bot.get_cog("Reminders")
        if not reminder_cog:
            await interaction.followup.send(
                "Reminders cog not loaded.", ephemeral=True
            )
            return

        mentions: list[str] = []
        try:
            async for user in event.users():
                mentions.append(user.mention)
        except Exception as exc:
            log.warning("Could not fetch participants: %s", exc)

        await reminder_cog._send_reminders(
            event,
            int(master_event_id),
            " ".join(mentions),
            mark_sent=False,
        )
        await interaction.followup.send("Reminder sent.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Admin(bot))
