from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from app.models import GarminAccount, User, Workout
from app.repositories.users import get_or_create_garmin_account
from app.repositories.workouts import find_workout
from app.services.garmin.client import GarminUnavailableError, connect_garmin_account
from app.services.garmin.locks import GarminAccountBusyError, garmin_account_slot
from app.services.garmin.workout_export import (
    delete_published_workout,
    push_workout,
    schedule_published_workout,
    update_published_workout,
    upload_workout,
)
from app.services.planning.validator import WorkoutInput, WorkoutValidationError, validate_workout
from app.services.planning.workout_definition import definition_to_json

type GarminConnector = Callable[[Session, GarminAccount], Any]


class WorkoutServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class WorkoutNotFoundError(WorkoutServiceError):
    def __init__(self) -> None:
        super().__init__("Workout nicht gefunden", code="workout.not_found")


class WorkoutTransitionError(WorkoutServiceError):
    pass


class WorkoutService:
    """User-scoped application service for the manual workout lifecycle."""

    def __init__(
        self,
        session: Session,
        user: User,
        *,
        connect_garmin: GarminConnector = connect_garmin_account,
    ) -> None:
        self.session = session
        self.user = user
        self.connect_garmin = connect_garmin

    def get(self, workout_id: int) -> Workout:
        workout = find_workout(self.session, self.user.id, workout_id)
        if workout is None:
            raise WorkoutNotFoundError
        return workout

    def validate(self, data: WorkoutInput) -> None:
        validate_workout(data)

    def create(self, data: WorkoutInput) -> Workout:
        self.validate(data)
        workout = Workout(
            user_id=self.user.id,
            name=data.name,
            sport=data.sport,
            scheduled_for=data.scheduled_for,
            description=data.description or None,
            status="draft",
            definition_version=1,
            definition=definition_to_json(data.definition),
        )
        self.session.add(workout)
        self.session.commit()
        return workout

    def update(self, workout_id: int, data: WorkoutInput) -> Workout:
        workout = self.get(workout_id)
        self.validate(data)
        previous_date = workout.scheduled_for
        self._apply_input(workout, data)
        try:
            if workout.garmin_workout_id:
                with self._garmin_client() as client:
                    update_published_workout(client, workout, previous_date)
                workout.status = "pushed"
            self.session.commit()
        except (GarminUnavailableError, WorkoutValidationError):
            self.session.rollback()
            raise
        return workout

    def confirm(self, workout_id: int) -> Workout:
        workout = self.get(workout_id)
        if workout.status == "draft":
            self.validate(self._workout_input(workout))
            workout.status = "confirmed"
            self.session.commit()
        return workout

    def publish(self, workout_id: int) -> Workout:
        workout = self.get(workout_id)
        if workout.status not in {"confirmed", "published", "pushed"}:
            raise WorkoutTransitionError(
                "Bitte den Entwurf vor der Übertragung bestätigen.",
                code="workout.confirmation_required",
            )
        try:
            with self._garmin_client() as client:
                if not workout.garmin_workout_id:
                    workout.garmin_workout_id = upload_workout(client, workout)
                    workout.status = "published"
                    # Keep the remote ID after a calendar failure so retries do not duplicate it.
                    self.session.commit()
                schedule_published_workout(client, workout)
                workout.status = "published"
                self.session.commit()
        except (GarminUnavailableError, WorkoutValidationError):
            self.session.rollback()
            raise
        return workout

    def push(self, workout_id: int) -> Workout:
        workout = self.get(workout_id)
        if workout.status not in {"published", "pushed"}:
            raise WorkoutTransitionError(
                "Das Workout muss vor der Übertragung veröffentlicht werden.",
                code="workout.publish_required",
            )
        try:
            with self._garmin_client() as client:
                push_workout(client, workout)
            workout.status = "pushed"
            self.session.commit()
        except (GarminUnavailableError, WorkoutValidationError):
            self.session.rollback()
            raise
        return workout

    def delete(self, workout_id: int) -> None:
        workout = self.get(workout_id)
        try:
            if workout.garmin_workout_id:
                with self._garmin_client() as client:
                    delete_published_workout(client, workout)
            self.session.delete(workout)
            self.session.commit()
        except (GarminUnavailableError, WorkoutValidationError):
            self.session.rollback()
            raise

    def _apply_input(self, workout: Workout, data: WorkoutInput) -> None:
        workout.name = data.name
        workout.sport = data.sport
        workout.scheduled_for = data.scheduled_for
        workout.description = data.description or None
        workout.definition_version = 1
        workout.definition = definition_to_json(data.definition)

    def _workout_input(self, workout: Workout) -> WorkoutInput:
        return WorkoutInput(
            name=workout.name,
            sport=workout.sport,
            scheduled_for=workout.scheduled_for,
            description=workout.description or "",
            definition=workout.definition_model,
        )

    @contextmanager
    def _garmin_client(self) -> Iterator[Any]:
        account = get_or_create_garmin_account(self.session, self.user)
        if account.connected_at is None:
            raise GarminUnavailableError("Garmin ist noch nicht verbunden.")
        try:
            with garmin_account_slot(account.id):
                yield self.connect_garmin(self.session, account)
        except GarminAccountBusyError as exc:
            raise GarminUnavailableError(
                "Für dieses Garmin-Konto läuft gerade eine andere Operation."
            ) from exc
