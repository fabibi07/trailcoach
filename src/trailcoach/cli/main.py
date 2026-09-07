"""CLI for TrailCoach."""

import json
from datetime import date, datetime
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
from trailcoach.training.engine import recalculate_athlete
from trailcoach.training.thresholds import set_threshold


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


@cli.command("set-threshold")
@click.option("--athlete-id", required=True, help="Athlete UUID.")
@click.option("--kind", required=True, help="Threshold kind (e.g. ftp_pace_mps, lthr_bpm).")
@click.option("--value", required=True, type=float, help="Threshold value.")
@click.option("--unit", default="", help="Unit of measurement.")
@click.option(
    "--valid-from",
    required=True,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Date from which the threshold is valid (YYYY-MM-DD).",
)
@click.option("--source", default="manual", help="Source of the value.")
@click.option("--notes", default=None, help="Optional notes.")
def set_threshold_cmd(
    athlete_id: str,
    kind: str,
    value: float,
    unit: str,
    valid_from: datetime,
    source: str,
    notes: str | None,
):
    """Insert or update a versioned athlete threshold."""
    db = SessionLocal()
    try:
        threshold = set_threshold(
            db,
            UUID(athlete_id),
            kind,
            value,
            unit,
            valid_from.date(),
            source=source,
            notes=notes,
        )
        db.commit()
        click.echo(f"Set {threshold.kind}={threshold.value} from {threshold.valid_from}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@cli.command("seed-thresholds")
@click.argument(
    "file_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--athlete-id", required=True, help="Athlete UUID.")
def seed_thresholds(file_path: Path, athlete_id: str):
    """Seed thresholds from a JSON file (list of {kind, value, unit, valid_from, ...})."""
    data = json.loads(file_path.read_text())
    db = SessionLocal()
    try:
        for item in data:
            set_threshold(
                db,
                UUID(athlete_id),
                item["kind"],
                float(item["value"]),
                item["unit"],
                date.fromisoformat(item["valid_from"]),
                source=item.get("source", "manual"),
                notes=item.get("notes"),
                confidence=item.get("confidence"),
            )
        db.commit()
        click.echo(f"Seeded {len(data)} thresholds for athlete {athlete_id}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@cli.command()
@click.option("--athlete-id", required=True, help="Athlete UUID.")
@click.option(
    "--from-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Only recompute from this date (YYYY-MM-DD).",
)
def recalculate(athlete_id: str, from_date: datetime | None):
    """Recompute activity loads, daily loads and PMC for the athlete."""
    db = SessionLocal()
    try:
        result = recalculate_athlete(
            db,
            UUID(athlete_id),
            from_date=from_date.date() if from_date else None,
            commit=True,
        )
        click.echo(f"Recalculated: {result}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
