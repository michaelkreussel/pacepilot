from collections.abc import Generator

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models import User


@pytest.fixture
def session_factory() -> Generator[sessionmaker[Session]]:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    factory = sessionmaker(bind=test_engine, expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Generator[TestClient]:
    def override_database() -> Generator[Session]:
        with session_factory() as session:
            yield session

    with session_factory() as session:
        user = User(display_name="Testathlet")
        session.add(user)
        session.commit()

    def override_current_user(request: Request) -> User:
        request.state.current_user = user
        return user

    get_settings().scheduler_enabled = False
    app.dependency_overrides[get_db] = override_database
    app.dependency_overrides[get_current_user] = override_current_user
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def unauthenticated_client(
    session_factory: sessionmaker[Session],
) -> Generator[TestClient]:
    def override_database() -> Generator[Session]:
        with session_factory() as session:
            yield session

    get_settings().scheduler_enabled = False
    app.dependency_overrides[get_db] = override_database
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
