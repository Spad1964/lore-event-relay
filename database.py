import logging
from typing import Optional

import aiosqlite

log = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS event_relay (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    master_event_id   TEXT    NOT NULL,
                    guild_id          TEXT    NOT NULL,
                    relay_event_id    TEXT    NOT NULL,
                    reminded          INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(master_event_id, guild_id)
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_master ON event_relay(master_event_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_relay ON event_relay(relay_event_id)"
            )
            await db.commit()
        log.info("Database ready: %s", self.path)

    # ── Write ────────────────────────────────────────────────────────────────

    async def add_relay(
        self,
        master_event_id: int,
        guild_id: int,
        relay_event_id: int,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO event_relay
                    (master_event_id, guild_id, relay_event_id, reminded)
                VALUES (?, ?, ?, 0)
                """,
                (str(master_event_id), str(guild_id), str(relay_event_id)),
            )
            await db.commit()

    async def mark_reminded(self, master_event_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE event_relay SET reminded = 1 WHERE master_event_id = ?",
                (str(master_event_id),),
            )
            await db.commit()

    async def delete_relays_for_master(self, master_event_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM event_relay WHERE master_event_id = ?",
                (str(master_event_id),),
            )
            await db.commit()

    async def delete_relay(self, master_event_id: int, guild_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM event_relay WHERE master_event_id = ? AND guild_id = ?",
                (str(master_event_id), str(guild_id)),
            )
            await db.commit()

    # ── Read ─────────────────────────────────────────────────────────────────

    async def get_relays_for_master(self, master_event_id: int) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM event_relay WHERE master_event_id = ?",
                (str(master_event_id),),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_relay_event_id(
        self, master_event_id: int, guild_id: int
    ) -> Optional[int]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                """SELECT relay_event_id FROM event_relay
                   WHERE master_event_id = ? AND guild_id = ?""",
                (str(master_event_id), str(guild_id)),
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else None

    async def get_unreminded_events(self) -> list[dict]:
        """Return distinct master_event_ids not yet reminded."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT DISTINCT master_event_id FROM event_relay WHERE reminded = 0"
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_all_relays(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM event_relay ORDER BY master_event_id"
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]