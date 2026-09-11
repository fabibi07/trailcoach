"""Create/migrate all database tables using Alembic."""

import click
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect

import trailcoach.db.models  # noqa: F401  ensures metadata is populated
from alembic import command as alembic_command
from trailcoach.core.config import settings
from trailcoach.db.base import Base


@click.command()
@click.option("--drop", is_flag=True, help="Drop existing tables before migrating.")
def main(drop: bool) -> None:
    engine = create_engine(str(settings.db_url))
    if drop:
        Base.metadata.drop_all(engine)

    if not drop and inspect(engine).has_table("alembic_version"):
        click.echo("Database already has alembic_version; run with --drop to recreate.")
        return

    alembic_cfg = AlembicConfig("alembic.ini")
    alembic_command.upgrade(alembic_cfg, "head")
    click.echo(f"Database migrated: {settings.db_url}")


if __name__ == "__main__":
    main()
