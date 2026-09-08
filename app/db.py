from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import DATABASE_URL
from app.context import identity

pool = AsyncConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=20,
    open=False,
    kwargs={"row_factory": dict_row},
)


async def set_tenant(conn: AsyncConnection, tenant_id: str) -> None:
    # set_config(..., true) is SET LOCAL: the value disappears at transaction end.
    await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))


async def get_conn() -> AsyncIterator[AsyncConnection]:
    request_identity = identity()
    async with pool.connection() as conn:
        async with conn.transaction():
            await set_tenant(conn, request_identity.tenant_id)
            yield conn


@asynccontextmanager
async def tenant_transaction(tenant_id: str):
    async with pool.connection() as conn:
        async with conn.transaction():
            await set_tenant(conn, tenant_id)
            yield conn
