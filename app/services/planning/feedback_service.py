from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    PostSessionFeedback,
    PreSessionFeedback,
    User,
    Workout,
    WorkoutValidationRun,
)
from app.models.user import utcnow
from app.services.planning.safety_triage import (
    PostSessionFeedbackInput,
    PreSessionFeedbackInput,
    feedback_content_hash,
)


class FeedbackNotFoundError(LookupError):
    pass


class FeedbackService:
    def __init__(self, session: Session, user: User) -> None:
        self.session = session
        self.user = user

    def record_pre_session(
        self, workout_id: int, data: PreSessionFeedbackInput
    ) -> PreSessionFeedback:
        workout = self.session.scalar(
            select(Workout).where(
                Workout.id == workout_id,
                Workout.user_id == self.user.id,
                Workout.deleted_at.is_(None),
            )
        )
        if workout is None:
            raise FeedbackNotFoundError("Workout nicht gefunden")
        pain = data.pain if data.pain.present else None
        feedback = PreSessionFeedback(
            user_id=self.user.id,
            workout_id=workout.id,
            workout_user_id=self.user.id,
            motivation=data.motivation,
            fatigue=data.fatigue,
            leg_freshness=data.leg_freshness,
            soreness=data.soreness,
            sleep_quality=data.sleep_quality,
            pain_present=data.pain.present,
            pain_location=pain.location or None if pain else None,
            pain_severity=pain.severity if pain else None,
            pain_alters_gait=pain.alters_gait if pain else None,
            pain_worsens_with_activity=pain.worsens_with_activity if pain else None,
            illness_signal=data.illness_signal.value,
            available_minutes=data.available_minutes,
            notes=data.notes or None,
            source="explicit_form",
            content_hash=feedback_content_hash(data),
        )
        self.session.add(feedback)
        self._invalidate_context(workout_ids={workout.id})
        self.session.commit()
        return feedback

    def record_post_session(
        self, activity_id: int, data: PostSessionFeedbackInput
    ) -> PostSessionFeedback:
        activity = self.session.scalar(
            select(Activity).where(Activity.id == activity_id, Activity.user_id == self.user.id)
        )
        if activity is None:
            raise FeedbackNotFoundError("Aktivität nicht gefunden")
        pain = data.pain if data.pain.present else None
        feedback = PostSessionFeedback(
            user_id=self.user.id,
            workout_id=activity.workout_id,
            workout_user_id=self.user.id if activity.workout_id is not None else None,
            activity_id=activity.id,
            activity_user_id=self.user.id,
            completion_percent=data.completion_percent,
            session_rpe=data.session_rpe,
            overall_feel=data.overall_feel,
            pain_present=data.pain.present,
            pain_location=pain.location or None if pain else None,
            pain_severity=pain.severity if pain else None,
            pain_alters_gait=pain.alters_gait if pain else None,
            pain_worsens_with_activity=pain.worsens_with_activity if pain else None,
            stopped_reason=data.stopped_reason or None,
            notes=data.notes or None,
            source="explicit_form",
            content_hash=feedback_content_hash(data),
        )
        self.session.add(feedback)
        workout_ids = {activity.workout_id} if activity.workout_id is not None else set()
        self._invalidate_context(workout_ids=workout_ids)
        self.session.commit()
        return feedback

    def pre_session_for_workout(self, workout_id: int) -> list[PreSessionFeedback]:
        return list(
            self.session.scalars(
                select(PreSessionFeedback)
                .where(
                    PreSessionFeedback.user_id == self.user.id,
                    PreSessionFeedback.workout_id == workout_id,
                )
                .order_by(PreSessionFeedback.recorded_at.desc(), PreSessionFeedback.id.desc())
            )
        )

    def post_session_for_activity(self, activity_id: int) -> list[PostSessionFeedback]:
        return list(
            self.session.scalars(
                select(PostSessionFeedback)
                .where(
                    PostSessionFeedback.user_id == self.user.id,
                    PostSessionFeedback.activity_id == activity_id,
                )
                .order_by(PostSessionFeedback.recorded_at.desc(), PostSessionFeedback.id.desc())
            )
        )

    def all_pre_session(self) -> list[PreSessionFeedback]:
        return list(
            self.session.scalars(
                select(PreSessionFeedback)
                .where(PreSessionFeedback.user_id == self.user.id)
                .order_by(PreSessionFeedback.recorded_at.desc(), PreSessionFeedback.id.desc())
            )
        )

    def all_post_session(self) -> list[PostSessionFeedback]:
        return list(
            self.session.scalars(
                select(PostSessionFeedback)
                .where(PostSessionFeedback.user_id == self.user.id)
                .order_by(PostSessionFeedback.recorded_at.desc(), PostSessionFeedback.id.desc())
            )
        )

    def delete_pre_session(self, feedback_id: int) -> int | None:
        feedback = self.session.scalar(
            select(PreSessionFeedback).where(
                PreSessionFeedback.id == feedback_id,
                PreSessionFeedback.user_id == self.user.id,
            )
        )
        if feedback is None:
            raise FeedbackNotFoundError("Feedback nicht gefunden")
        workout_id = feedback.workout_id
        self._purge_validation_evidence(f"pre:{feedback.id}")
        self.session.delete(feedback)
        self._invalidate_context(workout_ids={workout_id} if workout_id else set())
        self.session.commit()
        return workout_id

    def delete_post_session(self, feedback_id: int) -> int | None:
        feedback = self.session.scalar(
            select(PostSessionFeedback).where(
                PostSessionFeedback.id == feedback_id,
                PostSessionFeedback.user_id == self.user.id,
            )
        )
        if feedback is None:
            raise FeedbackNotFoundError("Feedback nicht gefunden")
        activity_id = feedback.activity_id
        workout_ids = {feedback.workout_id} if feedback.workout_id else set()
        self._purge_validation_evidence(f"post:{feedback.id}")
        self.session.delete(feedback)
        self._invalidate_context(workout_ids=workout_ids)
        self.session.commit()
        return activity_id

    def export_data(self) -> dict[str, object]:
        pre = list(
            self.session.scalars(
                select(PreSessionFeedback)
                .where(PreSessionFeedback.user_id == self.user.id)
                .order_by(PreSessionFeedback.id)
            )
        )
        post = list(
            self.session.scalars(
                select(PostSessionFeedback)
                .where(PostSessionFeedback.user_id == self.user.id)
                .order_by(PostSessionFeedback.id)
            )
        )
        return {
            "schema_version": "subjective-feedback-export.v1",
            "exported_at": utcnow().isoformat(timespec="seconds") + "Z",
            "pre_session_feedback": [self._pre_json(item) for item in pre],
            "post_session_feedback": [self._post_json(item) for item in post],
        }

    def _invalidate_context(self, *, workout_ids: set[int]) -> None:
        now = utcnow()
        user_workout_ids = select(Workout.id).where(Workout.user_id == self.user.id)
        self.session.execute(
            update(WorkoutValidationRun)
            .where(
                WorkoutValidationRun.workout_id.in_(user_workout_ids),
                WorkoutValidationRun.expires_at > now,
            )
            .values(expires_at=now)
        )
        if workout_ids:
            self.session.execute(
                update(Workout)
                .where(Workout.id.in_(workout_ids), Workout.user_id == self.user.id)
                .values(lock_version=Workout.lock_version + 1)
            )
        today = utcnow().date()
        self.session.execute(
            update(Workout)
            .where(
                Workout.user_id == self.user.id,
                Workout.scheduled_for == today,
                Workout.id.not_in(workout_ids),
                Workout.deleted_at.is_(None),
            )
            .values(lock_version=Workout.lock_version + 1)
        )

    def _purge_validation_evidence(self, feedback_reference: str) -> None:
        runs = list(
            self.session.scalars(
                select(WorkoutValidationRun)
                .join(Workout, Workout.id == WorkoutValidationRun.workout_id)
                .where(Workout.user_id == self.user.id)
            )
        )
        for run in runs:
            if feedback_reference in run.feedback_ids_json:
                self.session.delete(run)

    @staticmethod
    def _pain_json(item: PreSessionFeedback | PostSessionFeedback) -> dict[str, object]:
        return {
            "present": item.pain_present,
            "location": item.pain_location,
            "severity_0_10": item.pain_severity,
            "alters_gait": item.pain_alters_gait,
            "worsens_with_activity": item.pain_worsens_with_activity,
        }

    @classmethod
    def _pre_json(cls, item: PreSessionFeedback) -> dict[str, object]:
        return {
            "id": item.id,
            "workout_id": item.workout_id,
            "motivation_1_5": item.motivation,
            "fatigue_1_5": item.fatigue,
            "leg_freshness_1_5": item.leg_freshness,
            "soreness_0_10": item.soreness,
            "sleep_quality_1_5": item.sleep_quality,
            "pain": cls._pain_json(item),
            "illness_signal": item.illness_signal,
            "available_minutes": item.available_minutes,
            "notes": item.notes,
            "source": item.source,
            "recorded_at": item.recorded_at.isoformat(timespec="milliseconds") + "Z",
        }

    @classmethod
    def _post_json(cls, item: PostSessionFeedback) -> dict[str, object]:
        return {
            "id": item.id,
            "workout_id": item.workout_id,
            "activity_id": item.activity_id,
            "completion_percent": item.completion_percent,
            "session_rpe_0_10": item.session_rpe,
            "overall_feel_1_5": item.overall_feel,
            "pain": cls._pain_json(item),
            "stopped_reason": item.stopped_reason,
            "notes": item.notes,
            "source": item.source,
            "recorded_at": item.recorded_at.isoformat(timespec="milliseconds") + "Z",
        }
