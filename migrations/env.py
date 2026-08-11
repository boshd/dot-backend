import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from benji_api.config import get_settings
from benji_api.db.base import Base
from benji_api.models import (  # noqa: F401
    AgentFollowUp,
    AgentRun,
    AgentToolCall,
    AuthIdentity,
    Conversation,
    ConversationChannel,
    FinancialAccount,
    FinancialConnection,
    FinancialGoal,
    FinancialLinkSession,
    FinancialTransaction,
    GeneratedApp,
    GeneratedAppRecord,
    GeneratedAppVersion,
    IntegrationAccount,
    IntegrationConnectLink,
    IntegrationGrant,
    IntegrationOAuthState,
    IntegrationSubscription,
    MemoryEntity,
    MemoryEpisode,
    MemoryEvidence,
    MemoryFact,
    MemoryJob,
    Message,
    MessageDelivery,
    ScheduledTask,
    User,
    UserEvent,
    WebhookEvent,
)

config = context.config

if config.config_file_name is not None and config.get_section("loggers"):
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata
MIGRATION_ADVISORY_LOCK_ID = 7_862_928_105_202_608


def include_object(
    object_: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    del object_, reflected, compare_to
    return not (type_ == "index" and name == "ix_memory_facts_statement_fts")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    use_advisory_lock = connection.dialect.name == "postgresql"
    if use_advisory_lock:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": MIGRATION_ADVISORY_LOCK_ID},
        )
        # SQLAlchemy 2 starts a transaction for the lock query. Commit that transaction so
        # Alembic can own and commit the migration transaction below; the session-level advisory
        # lock remains held until it is explicitly released.
        connection.commit()
    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()
    finally:
        if use_advisory_lock:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": MIGRATION_ADVISORY_LOCK_ID},
            )
            connection.commit()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
