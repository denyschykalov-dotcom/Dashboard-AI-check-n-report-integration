from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from backend.app.config import get_settings
from backend.app.db import Base, build_engine

# Import models so their classes register on Base.metadata before create_all()
# runs on the non-Postgres fallback path below.
import backend.app.models  # noqa: F401


MIGRATIONS_TABLE = "backend_schema_migrations"


def run_pending_migrations() -> None:
    settings = get_settings()
    engine = build_engine(
        settings.migration_database_url,
        pool_mode=settings.db_pool_mode,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )

    if engine.dialect.name != "postgresql":
        Base.metadata.create_all(engine)
        return

    migrations_dir = Path(__file__).resolve().parent / "sql"
    migration_files = sorted(migrations_dir.glob("*.sql"))

    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                create table if not exists {MIGRATIONS_TABLE} (
                    name text primary key,
                    applied_at timestamptz not null default now()
                )
                """
            )
        )

        applied = {
            row[0]
            for row in connection.execute(text(f"select name from {MIGRATIONS_TABLE}")).fetchall()
        }

        for migration_file in migration_files:
            if migration_file.name in applied:
                continue
            sql = migration_file.read_text(encoding="utf-8")
            connection.execute(text(sql))
            connection.execute(
                text(f"insert into {MIGRATIONS_TABLE} (name) values (:name)"),
                {"name": migration_file.name},
            )


if __name__ == "__main__":
    run_pending_migrations()
