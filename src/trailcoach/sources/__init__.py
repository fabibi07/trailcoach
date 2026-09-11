"""Source adapter abstraction for TrailCoach."""

from trailcoach.sources.mock import MockSourceProvider
from trailcoach.sources.priority import resolve_source_for_metric
from trailcoach.sources.provider import HistoricalRange, ImportResult, SourceProvider
from trailcoach.sources.registry import SourceProviderRegistry

__all__ = [
    "HistoricalRange",
    "ImportResult",
    "MockSourceProvider",
    "SourceProvider",
    "SourceProviderRegistry",
    "resolve_source_for_metric",
]
