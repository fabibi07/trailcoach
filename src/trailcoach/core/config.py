from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    env: str = Field(default="development", alias="ENV")
    database_url: str = Field(
        default="postgresql+psycopg2://trailcoach:trailcoach@localhost:5432/trailcoach",
        alias="DATABASE_URL",
    )
    # Fallback a SQLite para desarrollo local rápido
    use_sqlite: bool = Field(default=False, alias="USE_SQLITE")
    sqlite_path: Path = Field(default=Path("data/trailcoach.db"), alias="SQLITE_PATH")

    raw_root: Path = Field(default=Path("data/raw"), alias="RAW_ROOT")
    processed_root: Path = Field(default=Path("data/processed"), alias="PROCESSED_ROOT")

    @property
    def raw_root_path(self) -> Path:
        return self.raw_root.expanduser().resolve()

    @property
    def processed_root_path(self) -> Path:
        return self.processed_root.expanduser().resolve()

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    encryption_key: bytes | None = Field(default=None, alias="ENCRYPTION_KEY")

    # AI Coach / LLM settings
    ai_provider: str = Field(default="openai", alias="AI_PROVIDER")
    ai_model: str = Field(default="gpt-4o-mini", alias="AI_MODEL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def db_url(self) -> str:
        if self.use_sqlite:
            path = self.sqlite_path.expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{path}"
        return self.database_url


settings = Settings()
