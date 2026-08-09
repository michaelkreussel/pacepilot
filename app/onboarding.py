from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.database import SessionDep
from app.models import User
from app.models.user import utcnow

CURRENT_ONBOARDING_VERSION = 1
OnboardingPhase = Literal["welcome", "connect", "sync", "complete"]


@dataclass(frozen=True)
class OnboardingState:
    phase: OnboardingPhase
    notice_acknowledged: bool
    connected: bool
    complete: bool
    planning_unlocked: bool
    data_unlocked: bool

    @property
    def step_number(self) -> int:
        return 1 if self.phase in {"welcome", "connect"} else 2


def onboarding_state(user: User) -> OnboardingState:
    account = user.garmin_account
    connected = account is not None and account.connected_at is not None
    complete = (
        user.onboarding_completed_at is not None
        and user.onboarding_completed_version >= CURRENT_ONBOARDING_VERSION
    )
    if complete:
        phase: OnboardingPhase = "complete"
    elif user.onboarding_notice_acknowledged_at is None:
        phase = "welcome"
    elif not connected:
        phase = "connect"
    else:
        phase = "sync"
    return OnboardingState(
        phase=phase,
        notice_acknowledged=user.onboarding_notice_acknowledged_at is not None,
        connected=connected,
        complete=complete,
        planning_unlocked=complete or connected,
        data_unlocked=complete,
    )


def complete_onboarding(session: Session, user_id: int) -> None:
    user = session.get(User, user_id)
    if user is None or (
        user.onboarding_completed_at is not None
        and user.onboarding_completed_version >= CURRENT_ONBOARDING_VERSION
    ):
        return
    now = utcnow()
    user.onboarding_notice_acknowledged_at = user.onboarding_notice_acknowledged_at or now
    user.onboarding_completed_at = now
    user.onboarding_completed_version = CURRENT_ONBOARDING_VERSION


class OnboardingAccessRequired(Exception):
    def __init__(self, area: str) -> None:
        self.area = area


def require_planning_access(session: SessionDep, user: CurrentUser) -> None:
    del session
    if not onboarding_state(user).planning_unlocked:
        raise OnboardingAccessRequired("planning")


def require_notice_acknowledged(session: SessionDep, user: CurrentUser) -> None:
    del session
    if not onboarding_state(user).notice_acknowledged:
        raise OnboardingAccessRequired("welcome")


def require_data_access(session: SessionDep, user: CurrentUser) -> None:
    del session
    if not onboarding_state(user).data_unlocked:
        raise OnboardingAccessRequired("data")


PlanningAccess = Annotated[None, Depends(require_planning_access)]
DataAccess = Annotated[None, Depends(require_data_access)]
