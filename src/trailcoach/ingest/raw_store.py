"""Immutable, content-addressable raw storage."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from trailcoach.core.config import settings
from trailcoach.db.models import RawFile


class RawStore:
    """Store raw files in an append-only, content-addressable layout.

    Layout:
        raw/<source>/<kind>/<year>/<month>/<sha256>[.<ext>]

    Files are never overwritten. A duplicate sha256 is a no-op idempotent
    ingestion and returns the existing row.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or settings.raw_root_path).expanduser().resolve()

    def store(
        self,
        db: Session,
        content: bytes,
        source: str,
        kind: str,
        athlete_id: UUID | None = None,
        source_activity_id: str | None = None,
        content_type: str | None = None,
        extension: str = "",
        captured_at: datetime | None = None,
    ) -> RawFile:
        sha256 = hashlib.sha256(content).hexdigest()
        existing = db.query(RawFile).filter(RawFile.sha256 == sha256).first()
        if existing:
            return existing

        ext = extension or ""
        dt = captured_at or datetime.now(timezone.utc)
        rel = Path(f"{source}/{kind}/{dt.year:04d}/{dt.month:02d}/{sha256}{ext}")
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)

        raw = RawFile(
            athlete_id=athlete_id,
            source=source,
            source_activity_id=source_activity_id,
            kind=kind,
            storage_path=str(rel),
            sha256=sha256,
            size_bytes=len(content),
            content_type=content_type,
            captured_at=captured_at,
        )
        db.add(raw)
        db.flush()
        return raw

    def store_file(
        self,
        db: Session,
        file_path: Path,
        source: str,
        kind: str,
        athlete_id: UUID | None = None,
        source_activity_id: str | None = None,
        content_type: str | None = None,
        extension: str | None = None,
        captured_at: datetime | None = None,
    ) -> RawFile:
        file_path = Path(file_path).expanduser().resolve()
        content = file_path.read_bytes()
        ext = extension
        if ext is None:
            ext = file_path.suffix
        return self.store(
            db=db,
            content=content,
            source=source,
            kind=kind,
            athlete_id=athlete_id,
            source_activity_id=source_activity_id,
            content_type=content_type,
            extension=ext,
            captured_at=captured_at,
        )

    def read_bytes(self, raw_file: RawFile) -> bytes:
        path = self.root / Path(raw_file.storage_path)
        return path.read_bytes()
