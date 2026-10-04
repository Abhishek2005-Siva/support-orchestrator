from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.models import Base, Meta

_engine: AsyncEngine | None = None
_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _maker
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        if _engine.dialect.name == "sqlite":
            @event.listens_for(_engine.sync_engine, "connect")
            def _pragmas(dbapi_conn, _):  # WAL = concurrent readers + 1 writer; busy_timeout avoids "database is locked"
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        _maker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


@asynccontextmanager
async def session_scope():
    get_engine()
    async with _maker() as s:  # type: ignore[misc]
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


async def init_db():
    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def reset_engine(url: str | None = None):
    """Test hook: point the app at a different database."""
    global _engine, _maker
    if _engine is not None:
        await _engine.dispose()
    _engine = _maker = None
    if url:
        get_settings().database_url = url


async def get_meta(key: str, default: str | None = None) -> str | None:
    async with session_scope() as s:
        row = (await s.execute(select(Meta).where(Meta.key == key))).scalar_one_or_none()
        return row.value if row else default
