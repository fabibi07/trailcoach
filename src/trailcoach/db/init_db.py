"""Create all database tables."""

import click
from sqlalchemy import create_engine

from trailcoach.core.config import settings
from trailcoach.db.base import Base


def create_tables():
    engine = create_engine(str(settings.db_url))
    Base.metadata.create_all(engine)
    return engine


@click.command()
@click.option("--drop", is_flag=True, help="Drop existing tables before creating.")
def main(drop: bool):
    engine = create_engine(str(settings.db_url))
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    click.echo(f"Database initialized: {settings.db_url}")


if __name__ == "__main__":
    main()
