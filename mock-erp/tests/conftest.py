from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    # In-memory SQLite, fresh schema per test -- fast, no Docker/Postgres needed
    # for unit tests. StaticPool so the single in-memory DB is shared across
    # connections within one test (SQLite's default would give each connection
    # its own separate, empty in-memory DB otherwise).
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    # Deliberately NOT using TestClient as a context manager here: that's what
    # triggers FastAPI's lifespan (startup/shutdown) events, and our lifespan
    # calls init_db()/seed() against the real Postgres engine configured in
    # app.db.session -- which isn't necessarily running during unit tests, and
    # tests should seed their own fixtures anyway, not depend on production
    # seed data.
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()
