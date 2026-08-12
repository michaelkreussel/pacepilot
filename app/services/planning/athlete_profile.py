from dataclasses import dataclass
from datetime import date

from app.services.planning.validator import SPORTS

EXPERIENCE_LEVELS = {"beginner", "intermediate", "advanced"}
ANCHOR_METHODS = {"manual", "field_test", "lab", "race"}
ANCHOR_RANGES: dict[str, tuple[float, float]] = {
    "max_hr": (80, 250),
    "threshold_hr": (80, 230),
    "threshold_pace_s_per_km": (120, 1_200),
    "running_threshold_power_watts": (50, 2_000),
    "cycling_ftp_watts": (50, 2_000),
    "reference_5k_seconds": (600, 28_800),
    "reference_10k_seconds": (1_200, 43_200),
    "reference_half_seconds": (2_400, 86_400),
    "reference_marathon_seconds": (4_800, 129_600),
}


class AthleteProfileValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GoalInput:
    sport: str
    event_name: str
    target_date: date
    distance_m: float | None
    target_duration_s: int | None


@dataclass(frozen=True)
class AvailabilityInput:
    weekday: int
    max_duration_minutes: int


@dataclass(frozen=True)
class ManualAnchorInput:
    sport: str
    metric: str
    value: float
    observed_on: date
    method: str = "manual"


@dataclass(frozen=True)
class AthleteProfileInput:
    primary_sport: str | None
    experience_level: str | None
    experience_years: int | None
    constraint_note: str
    constraint_until: date | None
    goal: GoalInput | None
    availability: tuple[AvailabilityInput, ...]
    anchors: tuple[ManualAnchorInput, ...]


def validate_athlete_profile(data: AthleteProfileInput, *, today: date | None = None) -> None:
    current_day = today or date.today()
    if data.primary_sport is not None and data.primary_sport not in SPORTS:
        raise AthleteProfileValidationError("Diese Hauptsportart wird noch nicht unterstützt.")
    if data.experience_level is not None and data.experience_level not in EXPERIENCE_LEVELS:
        raise AthleteProfileValidationError("Die Trainingserfahrung ist ungültig.")
    if data.experience_years is not None and not 0 <= data.experience_years <= 80:
        raise AthleteProfileValidationError("Trainingsjahre müssen zwischen 0 und 80 liegen.")
    if len(data.constraint_note) > 1_000:
        raise AthleteProfileValidationError(
            "Trainingshinweise dürfen höchstens 1000 Zeichen haben."
        )
    if data.constraint_until is not None and not data.constraint_note:
        raise AthleteProfileValidationError(
            "Bitte beschreibe die Einschränkung, wenn ein Enddatum angegeben ist."
        )

    if data.goal is not None:
        goal = data.goal
        if goal.sport not in SPORTS:
            raise AthleteProfileValidationError("Die Zielsportart wird noch nicht unterstützt.")
        if len(goal.event_name) > 200:
            raise AthleteProfileValidationError("Der Zielname darf höchstens 200 Zeichen haben.")
        if goal.target_date < current_day:
            raise AthleteProfileValidationError(
                "Das Zieldatum darf nicht in der Vergangenheit liegen."
            )
        if goal.distance_m is not None and not 100 <= goal.distance_m <= 1_000_000:
            raise AthleteProfileValidationError(
                "Die Zieldistanz muss zwischen 0,1 und 1000 km liegen."
            )
        if goal.target_duration_s is not None and not 60 <= goal.target_duration_s <= 604_800:
            raise AthleteProfileValidationError(
                "Die Zielzeit liegt außerhalb des gültigen Bereichs."
            )
        if goal.target_duration_s is not None and goal.distance_m is None:
            raise AthleteProfileValidationError(
                "Für eine Zielzeit ist eine Zieldistanz erforderlich."
            )

    weekdays: set[int] = set()
    for item in data.availability:
        if item.weekday in weekdays or not 0 <= item.weekday <= 6:
            raise AthleteProfileValidationError("Die verfügbaren Wochentage sind ungültig.")
        weekdays.add(item.weekday)
        if not 15 <= item.max_duration_minutes <= 1_440:
            raise AthleteProfileValidationError(
                "Die verfügbare Trainingsdauer muss zwischen 15 Minuten und 24 Stunden liegen."
            )

    anchor_keys: set[tuple[str, str]] = set()
    anchor_values: dict[tuple[str, str], float] = {}
    for anchor in data.anchors:
        key = (anchor.sport, anchor.metric)
        if anchor.sport not in SPORTS or key in anchor_keys:
            raise AthleteProfileValidationError("Die manuellen Leistungswerte sind ungültig.")
        anchor_keys.add(key)
        bounds = ANCHOR_RANGES.get(anchor.metric)
        if bounds is None or not bounds[0] <= anchor.value <= bounds[1]:
            raise AthleteProfileValidationError(
                "Mindestens ein manueller Leistungswert liegt außerhalb des gültigen Bereichs."
            )
        if anchor.observed_on > current_day:
            raise AthleteProfileValidationError(
                "Leistungswerte dürfen nicht aus der Zukunft stammen."
            )
        if anchor.method not in ANCHOR_METHODS:
            raise AthleteProfileValidationError("Die Quelle eines Leistungswerts ist ungültig.")
        anchor_values[key] = anchor.value

    if data.primary_sport is not None:
        max_hr = anchor_values.get((data.primary_sport, "max_hr"))
        threshold_hr = anchor_values.get((data.primary_sport, "threshold_hr"))
        if max_hr is not None and threshold_hr is not None and threshold_hr >= max_hr:
            raise AthleteProfileValidationError("Die Schwellen-HF muss unter der HFmax liegen.")
