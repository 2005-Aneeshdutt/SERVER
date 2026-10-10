from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(database_url: str, connect_timeout_s: float = 3.0) -> AsyncEngine:
    # A short connect timeout makes a dead database fail fast (and fall back / buffer)
    # instead of hanging requests.
    return create_async_engine(
        database_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        connect_args={"timeout": connect_timeout_s},
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
