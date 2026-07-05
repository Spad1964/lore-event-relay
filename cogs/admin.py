import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


class Admin(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    def _guild_permission_summary(self, guild: discord.Guild) -> str:
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

    def _format_audit_entry(self, row: dict) -> str:
        details = row.get("details") or ""
        parts = [row.get("created_at", "?")]
        if row.get("action"):
            parts.append(row["action"])
        if row.get("status"):
            parts.append(row["status"])
        if row.get("guild_id"):
            parts.append(f"guild={row['guild_id']}")
        if row.get("master_event_id"):
            parts.append(f"master={row['master_event_id']}")
        if row.get("relay_event_id"):
            parts.append(f"relay={row['relay_event_id']}")
        if details:
            parts.append(details)
        return " | ".join(parts)

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
    @app_commands.describe(dry_run="Preview the changes without creating relays")
    async def sync_events(
        self,
        interaction: discord.Interaction,
        dry_run: bool = False,
    ) -> None:
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

        created = await relay_cog.sync_missing_relays(dry_run=dry_run)
        skipped = 0

        await interaction.followup.send(
            (
                f"Sync dry-run: **{created}** relay(s) would be created."
                if dry_run
                else f"Sync finished: **{created}** created."
            ),
            ephemeral=True,
        )

    # ── /relay repair ────────────────────────────────────────────────────────

    @relay.command(
        name="repair",
        description="Verify relay events and recreate missing copies",
    )
    @app_commands.describe(dry_run="Preview the changes without recreating relays")
    async def repair(
        self,
        interaction: discord.Interaction,
        dry_run: bool = False,
    ) -> None:
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

        repaired = await relay_cog.repair_missing_relays(dry_run=dry_run)

        await interaction.followup.send(
            (
                f"Repair dry-run: **{repaired}** relay(s) would be recreated."
                if dry_run
                else f"Repair finished: **{repaired}** recreated."
            ),
            ephemeral=True,
        )

    # ── /relay cleanup ───────────────────────────────────────────────────────

    @relay.command(
        name="cleanup",
        description="Remove DB entries for events that no longer exist",
    )
    @app_commands.describe(dry_run="Preview the changes without deleting records")
    async def cleanup(
        self,
        interaction: discord.Interaction,
        dry_run: bool = False,
    ) -> None:
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
                if not dry_run:
                    await self.bot.db.delete_relays_for_master(mid)
                removed += 1

        await interaction.followup.send(
            (
                f"Cleanup dry-run: **{removed}** master event(s) would be removed."
                if dry_run
                else f"Cleanup: removed entries for **{removed}** master event(s)."
            ),
            ephemeral=True,
        )

    # ── /relay health ────────────────────────────────────────────────────────

    @relay.command(
        name="health",
        description="Show bot, guild, and database health for the relay system",
    )
    async def health(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="Relay Health",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )

        bot_user = self.bot.user
        embed.add_field(
            name="Bot",
            value=(
                f"{bot_user} | ready={self.bot.is_ready()} | latency={round(self.bot.latency * 1000)}ms"
                if bot_user
                else f"ready={self.bot.is_ready()} | latency={round(self.bot.latency * 1000)}ms"
            ),
            inline=False,
        )

        master_guild = self.bot.get_guild(self.bot.config.master_guild_id)
        embed.add_field(
            name="Master Guild",
            value=(
                f"**{master_guild.name}** (`{master_guild.id}`)"
                if master_guild
                else f"Not found (`{self.bot.config.master_guild_id}`)"
            ),
            inline=False,
        )

        target_lines = []
        for target in self.bot.config.target_guilds:
            guild = self.bot.get_guild(target.guild_id)
            if guild:
                target_lines.append(
                    f"• **{guild.name}** (`{guild.id}`) — {self._guild_permission_summary(guild)}"
                )
            else:
                target_lines.append(f"• Not found (`{target.guild_id}`)")

        embed.add_field(
            name=f"Target Guilds ({len(self.bot.config.target_guilds)})",
            value="\n".join(target_lines) if target_lines else "None configured",
            inline=False,
        )

        relays = await self.bot.db.get_all_relays()
        audit_rows = await self.bot.db.get_recent_audit_logs(limit=5)
        embed.add_field(name="Relay Rows", value=str(len(relays)), inline=True)
        embed.add_field(name="Audit Entries", value=str(len(audit_rows)), inline=True)

        if audit_rows:
            embed.add_field(
                name="Latest Audit",
                value="\n".join(self._format_audit_entry(row) for row in audit_rows[:3]),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /relay audit ─────────────────────────────────────────────────────────

    @relay.command(name="audit", description="Show recent relay audit entries")
    @app_commands.describe(limit="How many recent entries to show")
    async def audit(self, interaction: discord.Interaction, limit: int = 10) -> None:
        await interaction.response.defer(ephemeral=True)

        limit = max(1, min(limit, 25))
        rows = await self.bot.db.get_recent_audit_logs(limit=limit)

        if not rows:
            await interaction.followup.send("No audit entries found.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Relay Audit",
            color=discord.Color.dark_teal(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.description = "\n".join(self._format_audit_entry(row) for row in rows)

        await interaction.followup.send(embed=embed, ephemeral=True)

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

        relay_rows = await self.bot.db.get_relays_for_master(int(master_event_id))
        sent_count = await reminder_cog._send_reminders(
            event,
            int(master_event_id),
            relay_rows,
            mark_sent=False,
        )
        await interaction.followup.send(
            f"Reminder sent to **{sent_count}** target guild(s).",
            ephemeral=True,
        )


async def setup(bot) -> None:
    await bot.add_cog(Admin(bot))
