"""Apply append-only schema migrations before the bot starts."""

import logging
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)
MIGRATIONS = tuple(
    Path(__file__).with_name(name)
    for name in (
        "001_initial.sql",
        "002_current_fields.sql",
        "003_unique_steam_links.sql",
        "004_alert_delivery.sql",
        "005_delete_abandoned_families.sql",
        "006_member_publication.sql",
        "007_browser_sessions.sql",
        "008_game_statuses.sql",
        "009_owner_transfers.sql",
    )
)


async def migrate(connection: asyncpg.Connection) -> None:
    """Serialize startup and commit all pending migrations with their version records."""
    async with connection.transaction():
        # The lock is scoped to this database and released on commit or rollback.
        await connection.execute("SELECT pg_advisory_xact_lock($1)", 0x737465616D696C79)  # steamily
        if not await connection.fetchval("SELECT to_regclass('schema_migrations')"):
            await connection.execute("""
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)

        applied = await connection.fetch("SELECT version, name FROM schema_migrations ORDER BY version")
        expected = [(version, path.name) for version, path in enumerate(MIGRATIONS, 1)]
        if [tuple(row) for row in applied] != expected[: len(applied)]:
            raise RuntimeError("Database migration history does not match this bot release; use the matching release.")
        for version, path in enumerate(MIGRATIONS[len(applied) :], len(applied) + 1):
            log.info("Applying database migration %s", path.name)
            await connection.execute(path.read_text(encoding="utf-8"))
            await connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)", version, path.name
            )
