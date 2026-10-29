from alembic import context
from sqlalchemy import create_engine, pool

import jorgex_social.access.models  # noqa: F401  (registers tables on the metadata)
from jorgex_social.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_engine(config.get_main_option("sqlalchemy.url"), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # SQLite cannot alter most columns in place; batch mode recreates the table instead.
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported")
run_migrations_online()
