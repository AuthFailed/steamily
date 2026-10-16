import os
import unittest

import asyncpg
from cryptography.fernet import Fernet

from bot.db import Db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
needs_pg = unittest.skipUnless(TEST_DATABASE_URL, "set TEST_DATABASE_URL to run Postgres tests")


async def fresh_db() -> Db:
    """Empties the test database and returns a connected Db. Never point TEST_DATABASE_URL at real data."""
    connection = await asyncpg.connect(TEST_DATABASE_URL)
    await connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await connection.close()
    return await Db.connect(TEST_DATABASE_URL, Fernet.generate_key().decode())
