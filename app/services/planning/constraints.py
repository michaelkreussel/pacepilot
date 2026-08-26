from dataclasses import dataclass

from app.services.planning.registry import KnowledgeRegistry, get_knowledge_registry


@dataclass(frozen=True)
class LoadDimensions:
    duration_seconds: float
    distance_meters: float
    intensity_score: float
    density_score: float


def adaptation_does_not_increase_load(original: LoadDimensions, candidate: LoadDimensions) -> bool:
    return all(
        candidate_value <= original_value
        for original_value, candidate_value in zip(
            (
                original.duration_seconds,
                original.distance_meters,
                original.intensity_score,
                original.density_score,
            ),
            (
                candidate.duration_seconds,
                candidate.distance_meters,
                candidate.intensity_score,
                candidate.density_score,
            ),
            strict=True,
        )
    )


def has_single_session_distance_spike(
    candidate_distance_meters: float,
    longest_distance_last_30_days_meters: float | None,
    *,
    warning_ratio: float = 1.1,
) -> bool | None:
    if longest_distance_last_30_days_meters is None:
        return None
    if candidate_distance_meters <= 0 or longest_distance_last_30_days_meters <= 0:
        return False
    return candidate_distance_meters > longest_distance_last_30_days_meters * warning_ratio


def changes_at_most_one_progression_axis(
    original: LoadDimensions, candidate: LoadDimensions
) -> bool:
    volume_increased = (
        candidate.duration_seconds > original.duration_seconds
        or candidate.distance_meters > original.distance_meters
    )
    increases = sum(
        (
            volume_increased,
            candidate.intensity_score > original.intensity_score,
            candidate.density_score > original.density_score,
        )
    )
    return increases <= 1


def quality_spacing_requires_review(
    hours_since_last_quality: float | None,
    *,
    minimum_hours: float = 48,
) -> bool:
    return hours_since_last_quality is not None and hours_since_last_quality < minimum_hours


def catchup_stacking_is_allowed(requested: bool) -> bool:
    return not requested


class ConstraintEngine:
    def __init__(self, registry: KnowledgeRegistry | None = None) -> None:
        self.registry = registry or get_knowledge_registry()

    def adaptation_allows(self, original: LoadDimensions, candidate: LoadDimensions) -> bool:
        self._active_rule("ADAPT-NO-ESCALATION-001", "adaptation.no_load_increase")
        return adaptation_does_not_increase_load(original, candidate)

    def distance_spike_requires_review(
        self,
        candidate_distance_meters: float,
        longest_distance_last_30_days_meters: float | None,
    ) -> bool | None:
        rule = self._active_rule(
            "LOAD-SESSION-SPIKE-001", "progression.single_session_distance_spike"
        )
        warning_ratio = rule.parameters["warning_ratio"]
        if not isinstance(warning_ratio, float):
            raise ValueError("LOAD-SESSION-SPIKE-001.warning_ratio must be a float")
        return has_single_session_distance_spike(
            candidate_distance_meters,
            longest_distance_last_30_days_meters,
            warning_ratio=warning_ratio,
        )

    def progression_allows_one_axis(
        self, original: LoadDimensions, candidate: LoadDimensions
    ) -> bool:
        self._active_rule("LOAD-CHANGE-BUDGET-001", "progression.change_one_axis")
        return changes_at_most_one_progression_axis(original, candidate)

    def quality_spacing_requires_review(self, hours_since_last_quality: float | None) -> bool:
        rule = self._active_rule("DENSITY-HARD-DAYS-001", "quality.minimum_spacing")
        minimum_hours = rule.parameters["default_minimum_hours"]
        if not isinstance(minimum_hours, int) or isinstance(minimum_hours, bool):
            raise ValueError("DENSITY-HARD-DAYS-001.default_minimum_hours must be an integer")
        return quality_spacing_requires_review(
            hours_since_last_quality, minimum_hours=minimum_hours
        )

    def catchup_stacking_is_allowed(self, requested: bool) -> bool:
        self._active_rule("LOAD-NO-CATCHUP-001", "progression.no_catchup_stacking")
        return catchup_stacking_is_allowed(requested)

    def _active_rule(self, rule_id: str, implementation: str):
        rule = self.registry.constraints[rule_id]
        if rule.status != "active" or rule.implementation != implementation:
            raise ValueError(f"Constraint {rule_id} is not active with {implementation}")
        return rule
