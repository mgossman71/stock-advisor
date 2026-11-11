"""Migration integrity: the schema Alembic builds must match the ORM models.

This is the guard that makes "I changed a model and forgot a migration" fail
loudly in ``make test`` instead of surfacing as a runtime error in the UI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.config import BASE_DIR
from app.db.base import Base
import app.db.models  # noqa: F401 - registers every table on Base.metadata


@pytest.fixture
def alembic_config(tmp_path: Path) -> Config:
    """Alembic config pointed at a throwaway SQLite file."""
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'migrated.db'}")
    return config


def _schema(url: str) -> dict[str, set[str]]:
    """Table -> column-name sets as they exist in the database right now."""
    engine = create_engine(url, future=True)
    try:
        inspector = inspect(engine)
        return {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in inspector.get_table_names()
        }
    finally:
        engine.dispose()


def test_upgrade_head_creates_every_model_table(alembic_config: Config, tmp_path: Path):
    command.upgrade(alembic_config, "head")
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    built = _schema(url)
    expected = set(Base.metadata.tables) - {"alembic_version"}
    assert expected <= set(built), f"missing tables: {sorted(expected - set(built))}"


def test_migrated_columns_match_the_orm(alembic_config: Config, tmp_path: Path):
    """Every model column must exist in the migrated schema."""
    command.upgrade(alembic_config, "head")
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    built = _schema(url)
    for table_name, model_table in Base.metadata.tables.items():
        assert table_name in built, f"table {table_name} missing from the migration"
        model_columns = {column.name for column in model_table.columns}
        assert model_columns == built[table_name], (
            f"{table_name} drift - migration only: "
            f"{sorted(built[table_name] - model_columns)}, model only: "
            f"{sorted(model_columns - built[table_name])}"
        )


def test_upgrade_is_idempotent(alembic_config: Config, tmp_path: Path):
    """Running ``upgrade head`` twice must not error or double-apply."""
    command.upgrade(alembic_config, "head")
    command.upgrade(alembic_config, "head")
    built = _schema(f"sqlite:///{tmp_path / 'migrated.db'}")
    assert "price_history" in built


def test_downgrade_then_upgrade_round_trips(alembic_config: Config, tmp_path: Path):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    built = _schema(f"sqlite:///{tmp_path / 'migrated.db'}")
    assert "price_history" not in built
    command.upgrade(alembic_config, "head")
    assert "price_history" in _schema(f"sqlite:///{tmp_path / 'migrated.db'}")


def test_there_is_exactly_one_head():
    """Multiple heads mean a branch nobody merged - caught before it bites."""
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"expected a single head, found {heads}"
