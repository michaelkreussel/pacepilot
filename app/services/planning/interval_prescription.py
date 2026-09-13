"""Vary interval structure within an existing work allowance, never increase it for variety."""

from datetime import date
from math import ceil

from app.services.planning.registry_models import IntervalStructure
from app.services.planning.workout_definition import PaceRangeTarget
from app.services.planning.workout_templates import TemplateExpansionError, TemplateParameters


def interval_parameters(
    template_id: str,
    structure: IntervalStructure,
    base: TemplateParameters,
    pace: PaceRangeTarget | None,
    day: date,
    budget: int,
    requested_distance: int | None = None,
) -> TemplateParameters:
    work_allowance = base.work_allowance_seconds or (
        (base.repetitions or structure.repetitions.default)
        * (base.work_minutes or structure.work_minutes.default)
        * 60
    )
    # Calendar-week rotation is stable on reload and gives prospective plans variety.
    # Dose and pace still come from evidence, not the rotation.
    distances = (
        (400, 600, 800, 1000, None)
        if template_id == "vo2_intervals"
        else (1000, 1200, 1600, 2000, None)
    )
    index = (day.toordinal() // 7) % len(distances)
    options = (
        (requested_distance,)
        if requested_distance is not None
        else distances[index:] + distances[:index]
    )
    if requested_distance is not None and (requested_distance not in distances or pace is None):
        raise TemplateExpansionError(
            "Diese Distanz benötigt ein passendes Format und aktuelle Leistungsdaten "
            "für Zielpaces.",
            code="template.distance_pace_required",
        )
    warmup = 15 if template_id == "vo2_intervals" or work_allowance >= 1200 else 10
    cooldown = 10 if work_allowance >= 1200 else 5
    for distance in options:
        if distance is not None and pace is None:
            continue
        seconds = (
            ceil(distance * pace.slowest_seconds_per_km / 1000)
            if distance and pace
            else (base.work_minutes or structure.work_minutes.default) * 60
        )
        minimum = (
            60
            if distance and template_id == "vo2_intervals"
            else structure.work_minutes.minimum * 60
        )
        if not minimum <= seconds <= structure.work_minutes.maximum * 60:
            continue
        recovery = max(
            60,
            min(180, ceil(seconds * (0.75 if template_id == "vo2_intervals" else 0.25) / 30) * 30),
        )
        count = min(structure.repetitions.maximum, int(work_allowance // seconds))
        while count >= structure.repetitions.minimum:
            total = (warmup + cooldown) * 60 + count * (seconds + recovery)
            if (
                structure.total_work_minutes.minimum * 60 <= count * seconds <= work_allowance
                and total <= budget * 60
            ):
                return TemplateParameters(
                    repetitions=count,
                    work_minutes=None if distance else seconds // 60,
                    work_distance_meters=distance,
                    warmup_minutes=warmup,
                    cooldown_minutes=cooldown,
                    recovery_seconds=recovery,
                )
            count -= 1
    if requested_distance is not None:
        raise TemplateExpansionError(
            "Diese Wiederholungen passen mit Vorbereitung und Pausen "
            "nicht in den verfügbaren Umfang.",
            code="template.available_time_exceeded",
        )
    return base.model_copy(update={"vary_structure": False})
