"""Alembic environment.

The URL and the model metadata both come from the application, so migrations
always run against the same database the app uses.  ``compare_type=True`` makes
autogenerate notice column-type drift, which matters as the schema evolves
toward Postgres.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.db.base import Base
import app.db.models  # noqa: F401  - registers every table on Base.metadata

config = context.config
settings = get_settings()

# Keep .env authoritative unless a URL was explicitly provided to Alembic.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _render_item(type_, obj, autogen_context) -> str | bool:
    """Drop SQLite internal indexes from diffs; let Alembic render the rest.

    Returning ``False`` means "use the default renderer" - the documented
    contract for ``render_item`` hooks.
    """
    if type_ == "index" and str(getattr(obj, "name", "")).startswith("sqlite_"):
        return ""
    return False


def run_migrations_offline() -> None:
    """Emit SQL for a script without a live DB connection."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_item=_render_item,
            **({"render_as_batch": True} if connection.dialect.name == "sqlite" else {}),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
