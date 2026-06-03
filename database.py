import logging
from datetime import datetime, timezone
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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at      TEXT    NOT NULL,
                    action          TEXT    NOT NULL,
                    master_event_id TEXT,
                    guild_id        TEXT,
                    relay_event_id  TEXT,
                    details         TEXT
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)"
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
                INSERT INTO event_relay
                    (master_event_id, guild_id, relay_event_id, reminded)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(master_event_id, guild_id) DO UPDATE SET
                    relay_event_id = excluded.relay_event_id
                """,
                (str(master_event_id), str(guild_id), str(relay_event_id)),
            )
            await db.commit()

    async def record_audit(
        self,
        action: str,
        *,
        master_event_id: int | None = None,
        guild_id: int | None = None,
        relay_event_id: int | None = None,
        details: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO audit_log
                    (created_at, action, master_event_id, guild_id, relay_event_id, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    action,
                    str(master_event_id) if master_event_id is not None else None,
                    str(guild_id) if guild_id is not None else None,
                    str(relay_event_id) if relay_event_id is not None else None,
                    details,
                ),
            )
            await db.commit()

    async def log_audit(
        self,
        action: str,
        *,
        master_event_id: int | None = None,
        guild_id: int | None = None,
        relay_event_id: int | None = None,
        details: str | None = None,
    ) -> None:
        await self.record_audit(
            action,
            master_event_id=master_event_id,
            guild_id=guild_id,
            relay_event_id=relay_event_id,
            details=details,
        )

    async def mark_reminded(self, master_event_id: int, guild_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE event_relay
                SET reminded = 1
                WHERE master_event_id = ? AND guild_id = ?
                """,
                (str(master_event_id), str(guild_id)),
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

    async def mark_all_reminded(self, master_event_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE event_relay SET reminded = 1 WHERE master_event_id = ?",
                (str(master_event_id),),
            )
            await db.commit()

    async def get_unreminded_relays(self) -> list[dict]:
        """Return relay rows that have not received reminders yet."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT *
                FROM event_relay
                WHERE reminded = 0
                ORDER BY master_event_id, guild_id
                """
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_all_relays(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM event_relay ORDER BY master_event_id"
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_recent_audit_logs(self, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT *
                FROM audit_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
