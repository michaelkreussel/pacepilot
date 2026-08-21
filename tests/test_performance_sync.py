from datetime import date

from garminconnect.exceptions import GarminConnectNotFoundError
from sqlalchemy import select

from app.models import DailyFitness, GarminSyncState, User
from app.services.garmin.performance_sync import sync_performance_metrics


class FullPerformanceGarmin:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_fitnessage_data(self, value: str):
        self.calls.append("fitness_age")
        return {"calendarDate": value, "fitnessAge": 34.5}

    def get_endurance_score(self, value: str):
        self.calls.append("endurance_score")
        return {"calendarDate": value, "overallScore": 5_200}

    def get_hill_score(self, value: str):
        self.calls.append("hill_score")
        return {"calendarDate": value, "overallScore": 42}

    def get_lactate_threshold(self):
        self.calls.append("running_thresholds")
        return {
            "speed_and_heart_rate": {
                "calendarDate": "2026-06-29",
                "speed": 0.4,
                "heartRate": 171,
            },
            "power": {
                "calendarDate": "2026-06-28",
                "functionalThresholdPower": 315,
            },
        }

    def get_cycling_ftp(self):
        self.calls.append("cycling_ftp")
        return {"functionalThresholdPower": 285}

    def get_race_predictions(self):
        self.calls.append("race_predictions")
        return {
            "calendarDate": "2026-06-27",
            "time5K": 1_245,
            "time10K": 2_610,
            "timeHalfMarathon": 5_850,
            "timeMarathon": 12_420,
        }


def _user(session) -> User:
    user = User(display_name="Performance")
    session.add(user)
    session.flush()
    return user


def test_performance_sync_stores_sparse_values_on_their_source_days(session_factory):
    client = FullPerformanceGarmin()
    today = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        first = sync_performance_metrics(session, client, user.id, today=today, delay=0)

        assert first.api_calls == 6
        assert first.stored_values == 11
        assert {item.status for item in first.resources.values()} == {"ok"}
        rows = {
            row.day: row
            for row in session.scalars(
                select(DailyFitness)
                .where(DailyFitness.user_id == user.id)
                .order_by(DailyFitness.day)
            )
        }
        assert rows[date(2026, 6, 27)].race_prediction_5k_seconds == 1_245
        assert rows[date(2026, 6, 28)].running_ftp_watts == 315
        assert rows[date(2026, 6, 29)].lactate_threshold_speed_mps == 4.0
        assert rows[date(2026, 6, 29)].lactate_threshold_hr == 171
        assert rows[today].cycling_ftp_watts == 285
        assert rows[today].fitness_age == 34.5
        assert rows[today].endurance_score == 5_200
        assert rows[today].hill_score == 42

        call_count = len(client.calls)
        second = sync_performance_metrics(session, client, user.id, today=today, delay=0)
        assert len(client.calls) == call_count
        assert all(item.skipped for item in second.resources.values())


class PartialPerformanceGarmin(FullPerformanceGarmin):
    def get_endurance_score(self, value: str):
        return {}

    def get_hill_score(self, value: str):
        raise GarminConnectNotFoundError("API Error 404")

    def get_lactate_threshold(self):
        return {
            "speed_and_heart_rate": {"calendarDate": "2026-06-30"},
            "power": {"functionalThresholdPower": 300},
        }

    def get_cycling_ftp(self):
        return {"unexpected": 200}

    def get_race_predictions(self):
        return {"calendarDate": "2026-06-30", "time5K": 1_200}


def test_performance_sync_keeps_capabilities_independent(session_factory):
    with session_factory() as session:
        user = _user(session)
        result = sync_performance_metrics(
            session,
            PartialPerformanceGarmin(),
            user.id,
            today=date(2026, 6, 30),
            delay=0,
        )

        assert result.resources["fitness_age"].status == "ok"
        assert result.resources["endurance_score"].status == "empty"
        assert result.resources["hill_score"].status == "unsupported"
        assert result.resources["running_thresholds"].status == "partial"
        assert result.resources["cycling_ftp"].status == "schema_error"
        assert result.resources["race_predictions"].status == "partial"
        states = {
            state.resource: state
            for state in session.scalars(
                select(GarminSyncState).where(GarminSyncState.user_id == user.id)
            )
        }
        assert states["hill_score"].backfill_complete is True
        assert states["cycling_ftp"].error == "cycling-FTP value missing from payload"
        fitness = session.scalar(
            select(DailyFitness).where(
                DailyFitness.user_id == user.id,
                DailyFitness.day == date(2026, 6, 30),
            )
        )
        assert fitness is not None
        assert fitness.running_ftp_watts == 300
        assert fitness.race_prediction_5k_seconds == 1_200
        assert fitness.cycling_ftp_watts is None
