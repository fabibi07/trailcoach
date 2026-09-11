"""Data lineage helpers for TrailCoach."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass
class DataLineage:
    """Minimal provenance record attached to canonical model objects.

    All foreign-key references are optional because not every source can
    provide raw files, devices or account records.
    """

    source: str
    timestamp: datetime
    source_record_id: str | None = None
    raw_file_id: UUID | None = None
    device_id: UUID | None = None
    account_id: UUID | None = None
    metric: str | None = None
    value_type: str = "native"  # native | derived | estimated | unavailable
    confidence: float | None = None
    data_quality: str | None = None
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        result: dict[str, Any] = {}
        for key, value in asdict(self).items():
            if value is None:
                continue
            if isinstance(value, UUID):
                value = str(value)
            elif isinstance(value, datetime):
                value = value.isoformat()
            result[key] = value
        return result
