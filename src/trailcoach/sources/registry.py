"""Source provider registry used by the ingestion orchestrator."""


from trailcoach.sources.provider import SourceProvider


class SourceProviderRegistry:
    """Central registry of concrete SourceProvider implementations."""

    _providers: dict[str, type[SourceProvider]] = {}

    @classmethod
    def register(cls, provider_cls: type[SourceProvider]) -> type[SourceProvider]:
        """Register a provider class keyed by its source slug."""
        cls._providers[provider_cls.source] = provider_cls
        return provider_cls

    @classmethod
    def get(cls, source: str) -> type[SourceProvider] | None:
        """Return the registered provider class for a source slug."""
        return cls._providers.get(source)

    @classmethod
    def list_sources(cls) -> list[str]:
        """Return all registered source slugs."""
        return sorted(cls._providers.keys())
