from collections.abc import Generator
from typing import Annotated

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import main as main_module
from app.auth import get_current_user
from app.config import get_settings
from app.database import Base, get_db
from app.models import User
from app.models.user import utcnow

app = main_module.app


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
def client(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient]:
    def override_database() -> Generator[Session]:
        with session_factory() as session:
            yield session

    with session_factory() as session:
        completed_at = utcnow()
        user = User(
            display_name="Testathlet",
            onboarding_notice_acknowledged_at=completed_at,
            onboarding_completed_at=completed_at,
            onboarding_completed_version=1,
        )
        session.add(user)
        session.commit()
        user_id = user.id

    def override_current_user(
        request: Request, session: Annotated[Session, Depends(get_db)]
    ) -> User:
        user = session.get(User, user_id)
        assert user is not None
        request.state.current_user = user
        return user

    previous_overrides = dict(app.dependency_overrides)
    monkeypatch.setattr(get_settings(), "scheduler_enabled", False)
    monkeypatch.setattr(main_module, "upgrade_database", lambda: None)
    app.dependency_overrides[get_db] = override_database
    app.dependency_overrides[get_current_user] = override_current_user
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.fixture
def unauthenticated_client(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient]:
    def override_database() -> Generator[Session]:
        with session_factory() as session:
            yield session

    previous_overrides = dict(app.dependency_overrides)
    monkeypatch.setattr(get_settings(), "scheduler_enabled", False)
    monkeypatch.setattr(main_module, "upgrade_database", lambda: None)
    app.dependency_overrides[get_db] = override_database
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
