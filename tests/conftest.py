"""Shared pytest fixtures for TrailCoach tests."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from trailcoach.core.config import settings
from trailcoach.db.base import Base


@pytest.fixture(scope="session")
def engine():
    return create_engine("sqlite:///:memory:")


@pytest.fixture
def db(engine, tmp_path):
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()

    settings.raw_root = tmp_path / "raw"
    settings.processed_root = tmp_path / "processed"

    yield session

    session.close()
    Base.metadata.drop_all(engine)
