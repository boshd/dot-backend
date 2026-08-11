import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_local_locks: dict[UUID, asyncio.Lock] = {}


@asynccontextmanager
async def conversation_turn_lock(
    factory: async_sessionmaker[AsyncSession],
    *,
    conversation_id: UUID,
) -> AsyncIterator[None]:
    """Serialize agent wakes per conversation across processes on Postgres."""
    async with factory() as session:
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            lock_key = int.from_bytes(conversation_id.bytes[:8], byteorder="big", signed=True)
            await session.execute(
                text("SELECT pg_advisory_lock(:lock_key)"), {"lock_key": lock_key}
            )
            try:
                yield
            finally:
                await session.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key}
                )
            return

    lock = _local_locks.setdefault(conversation_id, asyncio.Lock())
    async with lock:
        yield
