from dataclasses import replace
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.config import get_settings
from app.models import Workout
from app.services.garmin.workout_export import compile_workout_with_report
from app.services.planning.constraints import (
    ConstraintEngine,
    LoadDimensions,
    adaptation_does_not_increase_load,
    changes_at_most_one_progression_axis,
    has_single_session_distance_spike,
)
from app.services.planning.validator import WorkoutInput, WorkoutValidationError, validate_workout
from app.services.planning.workout_definition import (
    DefinitionValidationError,
    RepeatBlockV2,
    definition_to_json,
    parse_definition,
    workout_metrics,
)
from app.services.planning.workout_revision import workout_content_hash
from app.services.planning.workout_templates import (
    TemplateEligibilityContext,
    TemplateExpansionError,
    TemplateParameters,
    expand_workout_template,
)


def _eligibility(*facts: str, available_minutes: int = 240) -> TemplateEligibilityContext:
    return TemplateEligibilityContext(
        consistent_running_weeks=52,
        runs_per_week=7,
        available_minutes=available_minutes,
        facts=set(facts),
    )


@pytest.mark.parametrize("template_id", ["easy_run", "recovery_run", "long_run", "strides"])
def test_active_templates_expand_deterministically(template_id: str) -> None:
    facts = {
        "recovery_run": ("easy_running_is_habitual_and_recovery_supportive",),
        "long_run": ("sufficient_recent_long_run_baseline",),
        "strides": ("familiar_with_relaxed_fast_running",),
    }.get(template_id, ())
    eligibility = _eligibility(*facts)
    first = expand_workout_template(template_id, eligibility=eligibility)
    second = expand_workout_template(template_id, eligibility=eligibility)

    assert first.canonical_json() == second.canonical_json()
    assert first.definition_version == 2
    assert first.evidence_refs
    assert first.load_estimate.distance_meters is None
    assert first.load_estimate.uncertainty
    assert workout_metrics(first.definition).duration_seconds > 0


def test_deferred_and_out_of_range_templates_fail_explicitly() -> None:
    with pytest.raises(TemplateExpansionError) as deferred:
        expand_workout_template("threshold_cruise", eligibility=_eligibility())
    with pytest.raises(TemplateExpansionError) as out_of_range:
        expand_workout_template(
            "easy_run",
            TemplateParameters(duration_minutes=120),
            eligibility=_eligibility(),
        )

    assert deferred.value.code == "template.not_active"
    assert out_of_range.value.code == "template.parameter_out_of_range"


@pytest.mark.parametrize(
    ("template_id", "expected_seconds", "work_domain"),
    [
        ("threshold_cruise", 57 * 60, "moderate"),
        ("vo2_intervals", 60 * 60, "high"),
    ],
)
def test_deferred_quality_templates_expand_in_development_test_mode(
    template_id: str,
    expected_seconds: int,
    work_domain: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_deferred_quality_templates_enabled", True)
    expanded = expand_workout_template(
        template_id,
        eligibility=_eligibility(
            "reliable_intensity_model",
            "reliable_current_performance_model",
            "quality_density_validation",
        ),
        allow_deferred_quality=True,
    )

    assert expanded.load_estimate.duration_seconds == expected_seconds
    assert len(expanded.definition.blocks) == 3
    assert isinstance(expanded.definition.blocks[1], RepeatBlockV2)
    domains = expanded.load_estimate.time_by_intensity_domain_seconds
    assert getattr(domains, work_domain) > 0
    assert domains.low + domains.moderate + domains.high == expected_seconds


def test_deferred_quality_expansion_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "coach_deferred_quality_templates_enabled", True)
    with pytest.raises(TemplateExpansionError) as blocked:
        expand_workout_template(
            "threshold_cruise",
            eligibility=_eligibility("reliable_intensity_model", "quality_density_validation"),
        )
    assert blocked.value.code == "template.not_active"


def test_v1_v2_parsing_and_hash_include_local_guidance() -> None:
    expanded = expand_workout_template("easy_run", eligibility=_eligibility())
    payload = definition_to_json(expanded.definition)
    parsed = parse_definition(payload, 2)
    workout = WorkoutInput(
        name="Easy Run",
        sport="running",
        scheduled_for=date(2026, 8, 25),
        description="",
        definition=parsed,
        definition_version=2,
    )
    validate_workout(workout)
    changed_payload = definition_to_json(parsed)
    changed_payload["blocks"][0]["instructions"][0] = "Andere lokale Anweisung"
    changed = replace(workout, definition=parse_definition(changed_payload, 2))

    assert workout_content_hash(workout) != workout_content_hash(changed)
    with pytest.raises(ValidationError):
        parse_definition(payload, 1)
    with pytest.raises(DefinitionValidationError):
        parse_definition(payload, 3)
    with pytest.raises(WorkoutValidationError) as mismatch:
        validate_workout(replace(workout, definition_version=1))
    assert mismatch.value.code == "definition.version_mismatch"


def test_garmin_compiler_degrades_rpe_and_instructions_explicitly() -> None:
    expanded = expand_workout_template("easy_run", eligibility=_eligibility())
    workout = Workout(
        user_id=1,
        name="Easy Run",
        sport="running",
        status="confirmed",
        definition_version=2,
        definition=definition_to_json(expanded.definition),
    )

    result = compile_workout_with_report(workout)

    target = result.payload["workoutSegments"][0]["workoutSteps"][0]["targetType"]
    assert target["workoutTargetTypeKey"] == "no.target"
    assert {warning.code for warning in result.warnings} == {
        "garmin.rpe_target_degraded",
        "garmin.instructions_omitted",
    }


@given(
    duration=st.floats(min_value=0, max_value=100_000, allow_nan=False),
    distance=st.floats(min_value=0, max_value=1_000_000, allow_nan=False),
    intensity=st.floats(min_value=0, max_value=10, allow_nan=False),
    density=st.floats(min_value=0, max_value=10, allow_nan=False),
)
def test_daily_adaptation_cannot_increase_any_load_dimension(
    duration: float, distance: float, intensity: float, density: float
) -> None:
    engine = ConstraintEngine()
    original = LoadDimensions(duration, distance, intensity, density)
    lower = LoadDimensions(duration / 2, distance / 2, intensity / 2, density / 2)

    assert adaptation_does_not_increase_load(original, lower)
    assert engine.adaptation_allows(original, lower)
    assert not adaptation_does_not_increase_load(
        original,
        LoadDimensions(duration + 1, distance, intensity, density),
    )


def test_progression_helpers_keep_risk_markers_conservative() -> None:
    engine = ConstraintEngine()
    baseline = LoadDimensions(3600, 10_000, 3, 1)

    assert has_single_session_distance_spike(11_001, 10_000)
    assert engine.distance_spike_requires_review(11_001, 10_000)
    assert not has_single_session_distance_spike(11_000, 10_000)
    assert has_single_session_distance_spike(11_000, None) is None
    assert changes_at_most_one_progression_axis(baseline, LoadDimensions(4200, 10_000, 3, 1))
    assert not changes_at_most_one_progression_axis(baseline, LoadDimensions(4200, 10_000, 4, 1))
    assert engine.progression_allows_one_axis(baseline, LoadDimensions(4200, 11_000, 3, 1))
    assert engine.quality_spacing_requires_review(24)
    assert not engine.quality_spacing_requires_review(None)
    assert not engine.catchup_stacking_is_allowed(True)


def test_template_eligibility_and_time_budget_are_enforced() -> None:
    with pytest.raises(TemplateExpansionError) as missing_baseline:
        expand_workout_template("long_run", eligibility=_eligibility())
    with pytest.raises(TemplateExpansionError) as safety_stop:
        expand_workout_template(
            "easy_run",
            eligibility=TemplateEligibilityContext(
                consistent_running_weeks=52,
                runs_per_week=7,
                available_minutes=60,
                safety_stop=True,
            ),
        )
    with pytest.raises(TemplateExpansionError) as time_budget:
        expand_workout_template(
            "easy_run",
            eligibility=_eligibility(available_minutes=30),
        )

    assert missing_baseline.value.code == "template.requirements_missing"
    assert safety_stop.value.code == "template.safety_stop"
    assert time_budget.value.code == "template.available_time_exceeded"
