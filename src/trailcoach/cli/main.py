"""CLI for TrailCoach."""

from pathlib import Path
from uuid import UUID

import click
from sqlalchemy import create_engine

from trailcoach.core.config import settings
from trailcoach.db.base import Base
from trailcoach.db.models import Athlete
from trailcoach.db.session import SessionLocal
from trailcoach.ingest.raw_store import RawStore
from trailcoach.providers.fit_parser import FitParser


@click.group()
def cli():
    """TrailCoach command line interface."""
    pass


@cli.command()
@click.option("--drop", is_flag=True, help="Drop existing tables.")
def init_db(drop: bool):
    """Create database tables."""
    engine = create_engine(str(settings.db_url))
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    click.echo(f"Database initialized at {settings.db_url}")


@cli.command()
@click.option("--name", required=True, help="Athlete display name.")
@click.option("--sex", type=click.Choice(["M", "F", None]), default=None, help="Sex (M/F).")
@click.option("--tz", default="UTC", help="Default timezone.")
def create_athlete(name: str, sex: str | None, tz: str):
    """Create the (single) athlete record."""
    db = SessionLocal()
    try:
        athlete = Athlete(display_name=name, sex=sex, tz_default=tz)
        db.add(athlete)
        db.commit()
        click.echo(f"Created athlete {athlete.id}")
    finally:
        db.close()


@cli.command()
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option("--athlete-id", required=True, help="Athlete UUID.")
@click.option("--dry-run", is_flag=True, help="Parse but do not write to DB.")
@click.option("--ext", default=".fit", help="File extension to ingest.")
def ingest_fitfiles(path: Path, athlete_id: str, dry_run: bool, ext: str):
    """Ingest all .fit files from a directory."""
    athlete_uuid = UUID(athlete_id)
    store = RawStore()
    parser = FitParser(store)
    db = SessionLocal()
    files = sorted(path.rglob(f"*{ext}"))
    if not files:
        click.echo(f"No {ext} files found in {path}")
        return

    click.echo(f"Found {len(files)} files.")
    created = 0
    existing = 0
    failed = 0
    try:
        for f in files:
            try:
                raw = store.store_file(
                    db=db,
                    file_path=f,
                    source="fitfile",
                    kind="fit",
                    athlete_id=athlete_uuid,
                    extension=".fit",
                )
                if dry_run:
                    click.echo(f"[dry-run] {f}")
                    continue
                parsed = parser.parse_to_source_activity(db, raw, athlete_uuid)
                activity_id = parsed.activity.id if parsed.activity else "n/a"
                click.echo(f"  ok: {f.name} -> {activity_id}")
                created += 1
            except Exception as e:
                failed += 1
                click.echo(f"  FAIL: {f.name} -> {e}", err=True)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    click.echo(f"Created {created}, existing {existing}, failed {failed}.")


@cli.command()
@click.option("--host", default="0.0.0.0")
@click.option("--port", default=8000, type=int)
@click.option("--reload", is_flag=True)
def serve(host: str, port: int, reload: bool):
    """Run the FastAPI development server."""
    import uvicorn

    uvicorn.run("trailcoach.api.main:app", host=host, port=port, reload=reload)


def main():
    cli()


if __name__ == "__main__":
    main()
