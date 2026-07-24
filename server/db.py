"""asyncpg pool + idempotent self-migrator (tracked in mikevideo._migrations)."""
import json
import logging
from pathlib import Path

import asyncpg

from . import config

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def _init_conn(con: asyncpg.Connection) -> None:
    """Every pooled connection encodes/decodes json & jsonb as Python objects,
    so we can pass dicts straight into a jsonb column (e.g. the ffprobe dump)."""
    await con.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                             schema="pg_catalog")
    await con.set_type_codec("json", encoder=json.dumps, decoder=json.loads,
                             schema="pg_catalog")


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1,
                                          max_size=8, command_timeout=60,
                                          init=_init_conn)
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db pool not initialised")
    return _pool


async def migrate() -> None:
    """Apply migrations/*.sql once each, tracked in mikevideo._migrations."""
    p = await connect()
    async with p.acquire() as con:
        await con.execute("CREATE SCHEMA IF NOT EXISTS mikevideo")
        await con.execute(
            "CREATE TABLE IF NOT EXISTS mikevideo._migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            name = sql_file.name
            done = await con.fetchval(
                "SELECT 1 FROM mikevideo._migrations WHERE name=$1", name)
            if done:
                continue
            logger.info("applying migration %s", name)
            sql = sql_file.read_text()
            async with con.transaction():
                await con.execute(sql)
                await con.execute(
                    "INSERT INTO mikevideo._migrations(name) VALUES($1)", name)
    logger.info("migrations up to date")
