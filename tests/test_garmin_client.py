from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.services.garmin import client as client_module


class FakeTokenClient:
    def __init__(self) -> None:
        self.dump_paths: list[Path] = []

    def dump(self, path: str) -> None:
        self.dump_paths.append(Path(path))


class FakeGarmin:
    instances: list["FakeGarmin"] = []
    needs_mfa = False

    def __init__(
        self,
        email: str | None,
        password: str | None,
        *,
        return_on_mfa: bool = False,
    ) -> None:
        self.email = email
        self.password = password
        self.return_on_mfa = return_on_mfa
        self.client = FakeTokenClient()
        self.login_paths: list[str | None] = []
        self.resume_calls: list[tuple[dict[str, Any], str]] = []
        self.instances.append(self)

    def login(self, path: str | None = None) -> tuple[str | None, None]:
        self.login_paths.append(path)
        return ("needs_mfa" if self.needs_mfa else None, None)

    def resume_login(self, state: dict[str, Any], code: str) -> tuple[None, None]:
        self.resume_calls.append((state, code))
        return None, None


@pytest.fixture(autouse=True)
def reset_fake_garmin() -> None:
    FakeGarmin.instances.clear()
    FakeGarmin.needs_mfa = False


def test_standard_login_persists_tokens_without_loading_cached_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(get_settings(), "garmin_token_dir", tmp_path)
    monkeypatch.setattr(client_module, "Garmin", FakeGarmin)

    challenge_id = client_module.start_garmin_login(
        "runner@example.com", "secret", account_id=3, user_id=7
    )

    garmin = FakeGarmin.instances[0]
    assert challenge_id is None
    assert garmin.login_paths == [None]
    assert garmin.client.dump_paths == [tmp_path / "account-3"]


def test_mfa_login_resumes_same_client_and_persists_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(get_settings(), "garmin_token_dir", tmp_path)
    monkeypatch.setattr(client_module, "Garmin", FakeGarmin)
    FakeGarmin.needs_mfa = True

    challenge_id = client_module.start_garmin_login(
        "runner@example.com", "secret", account_id=4, user_id=8
    )

    assert challenge_id is not None
    garmin = FakeGarmin.instances[0]
    assert garmin.password is None
    assert client_module.pending_garmin_login(challenge_id, account_id=4, user_id=8)
    assert not client_module.pending_garmin_login(challenge_id, account_id=4, user_id=9)

    email = client_module.finish_garmin_login(challenge_id, " 123456 ", account_id=4, user_id=8)

    assert email == "runner@example.com"
    assert garmin.resume_calls == [({}, "123456")]
    assert garmin.client.dump_paths == [tmp_path / "account-4"]
    assert not client_module.pending_garmin_login(challenge_id, account_id=4, user_id=8)


def test_mfa_challenge_expires(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = 100.0
    monkeypatch.setattr(get_settings(), "garmin_token_dir", tmp_path)
    monkeypatch.setattr(client_module, "Garmin", FakeGarmin)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: now)
    FakeGarmin.needs_mfa = True
    challenge_id = client_module.start_garmin_login(
        "runner@example.com", "secret", account_id=5, user_id=9
    )
    assert challenge_id is not None

    now += client_module.MFA_CHALLENGE_TTL_SECONDS + 1

    with pytest.raises(client_module.GarminMfaExpiredError):
        client_module.finish_garmin_login(challenge_id, "123456", account_id=5, user_id=9)
