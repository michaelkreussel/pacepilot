from typing import Any

from app.services.analytics.athlete_data import PlanningContext
from app.web import format_distance, format_duration, format_pace

SPORT_LABELS = {
    "running": "Laufen",
    "cycling": "Radfahren",
    "walking": "Gehen",
    "hiking": "Wandern",
    "general": "Allgemein",
}
EXPERIENCE_LABELS = {
    "beginner": "Einsteiger",
    "intermediate": "Fortgeschritten",
    "advanced": "Erfahren",
}
METRIC_LABELS = {
    "resting_hr_baseline": "Ruhepuls-Basis",
    "vo2max": "VO2max",
    "max_hr": "HFmax",
    "threshold_hr": "Schwellen-HF",
    "threshold_pace_s_per_km": "Schwellenpace",
    "running_threshold_power_watts": "Running Power Threshold",
    "cycling_ftp_watts": "Cycling FTP",
    "reference_1k_seconds": "1-km-Referenz",
    "reference_5k_seconds": "5-km-Referenz",
    "reference_10k_seconds": "10-km-Referenz",
    "reference_half_seconds": "Halbmarathon-Referenz",
    "reference_marathon_seconds": "Marathon-Referenz",
    "prediction_5k_seconds": "Garmin 5-km-Prognose",
    "prediction_10k_seconds": "Garmin 10-km-Prognose",
    "prediction_half_seconds": "Garmin Halbmarathon-Prognose",
    "prediction_marathon_seconds": "Garmin Marathon-Prognose",
}
SOURCE_LABELS = {"athlete": "Von dir", "garmin": "Garmin", "pacepilot": "PacePilot"}
FRESHNESS_LABELS = {
    "current": "Aktuell",
    "aging": "Bitte bald prüfen",
    "stale": "Veraltet",
    "unknown": "Stand unbekannt",
}
CONFIDENCE_LABELS = {
    "declared": "Selbst angegeben",
    "reported": "Vom Anbieter gemeldet",
    "high": "Hohe Datenbasis",
    "medium": "Mittlere Datenbasis",
    "low": "Geringe Datenbasis",
}
WEEKDAY_LABELS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def _display_number(value: float, decimals: int = 0) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def _metric_value(key: str, value: float, unit: str) -> str:
    if key == "threshold_pace_s_per_km":
        return format_pace(value)
    if key.startswith(("reference_", "prediction_")):
        return format_duration(value)
    if unit in {"bpm", "W"}:
        return f"{round(value)} {unit}"
    if key == "vo2max":
        return f"{_display_number(value, 1)} ml/kg/min"
    return f"{_display_number(value, 1)} {unit}"


def planning_view(planning: PlanningContext) -> dict[str, Any]:
    goal = planning.goal
    automatic = planning.automatic_profile
    state = automatic.performance_state
    zone_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for zone in planning.zones:
        zone_groups.setdefault((zone.sport, zone.zone_type), []).append(
            {
                "number": zone.zone_number,
                "lower": round(zone.lower_boundary),
                "upper": round(zone.upper_boundary) if zone.upper_boundary is not None else None,
            }
        )
    performance_items = [
        {
            "key": item.key,
            "label": METRIC_LABELS.get(item.key, item.key),
            "value": _metric_value(item.key, item.value, item.unit),
            "sport": SPORT_LABELS.get(item.sport or "", item.sport),
            "source": SOURCE_LABELS[item.source],
            "source_day": item.source_day,
            "freshness": FRESHNESS_LABELS[item.freshness],
            "confidence": CONFIDENCE_LABELS[item.confidence],
            "tone": "caution" if item.freshness in {"aging", "stale"} else "neutral",
        }
        for item in planning.performance
    ]
    overview_keys = {
        "threshold_pace_s_per_km",
        "threshold_hr",
        "running_threshold_power_watts",
        "vo2max",
        "reference_10k_seconds",
        "prediction_10k_seconds",
    }
    overview_performance = [item for item in performance_items if item["key"] in overview_keys][:4]
    finding_labels = {
        "training_consistency": "Regelmäßige Trainingskontinuität",
        "aerobic_durability": "Aerobe Stabilität bei längeren Läufen",
        "aerobic_durability_unknown": "Zu wenig gleichmäßige Läufe für HF-Drift",
        "threshold_evidence_missing": "Keine aktuelle Schwellen-Evidenz",
        "training_history_partial": "Trainingshistorie deckt den Zeitraum nicht vollständig ab",
        "weekly_capacity_unknown": "Nachhaltiger Wochenumfang noch unbekannt",
        "heart_rate_coverage_insufficient": "Herzfrequenz-Abdeckung nicht ausreichend",
    }

    def finding_view(item: Any) -> dict[str, Any]:
        observed = None
        if item.observed_value is not None:
            observed = (
                f"{_display_number(item.observed_value, 1)}%"
                if item.unit == "percent"
                else f"{round(item.observed_value)} von 12 Wochen"
                if item.unit == "weeks"
                else _display_number(item.observed_value, 1)
            )
        return {"label": finding_labels[item.key], "observed": observed}

    trend_labels = {
        "higher": "höher",
        "stable": "stabil",
        "lower": "niedriger",
        "unknown": "unbekannt",
    }
    trends = [
        {
            "weeks": item.horizon_weeks,
            "covered": item.covered,
            "earlier_distance": (
                format_distance(item.earlier_weekly_distance_m)
                if item.earlier_weekly_distance_m is not None
                else None
            ),
            "recent_distance": (
                format_distance(item.recent_weekly_distance_m)
                if item.recent_weekly_distance_m is not None
                else None
            ),
            "distance_change": (
                f"{item.distance_change_percent:+.1f}%".replace(".", ",")
                if item.distance_change_percent is not None
                else None
            ),
            "distance_direction": trend_labels[item.distance_direction],
            "earlier_duration": (
                format_duration(item.earlier_weekly_duration_s)
                if item.earlier_weekly_duration_s is not None
                else None
            ),
            "recent_duration": (
                format_duration(item.recent_weekly_duration_s)
                if item.recent_weekly_duration_s is not None
                else None
            ),
            "duration_direction": trend_labels[item.duration_direction],
            "earlier_sessions": item.earlier_sessions_per_week,
            "recent_sessions": item.recent_sessions_per_week,
            "sessions_direction": trend_labels[item.sessions_direction],
            "earlier_threshold": (
                format_pace(item.earlier_threshold_pace_s_per_km)
                if item.earlier_threshold_pace_s_per_km is not None
                else None
            ),
            "recent_threshold": (
                format_pace(item.recent_threshold_pace_s_per_km)
                if item.recent_threshold_pace_s_per_km is not None
                else None
            ),
            "threshold_direction": trend_labels[item.threshold_pace_direction],
        }
        for item in state.trends
    ]
    limits = planning.planning_limits
    target_labels = {
        "easy": "Easy Run",
        "tempo": "Tempolauf",
        "interval": "Intervalle",
        "long_run": "Long Run",
    }
    target_source_labels = {
        "empirical": "Empirisch",
        "empirical_easy_fallback": "Aus empirischem Easy-Run-Bereich",
        "threshold_fallback": "Aus Schwellenpace",
        "heart_rate_zones": "Aus Garmin-Zonen",
        "unavailable": "Keine Evidenz",
    }
    adjustment_labels = {
        "recovery_reduced_low": "Niedrige aktuelle Erholung: Umfang und Intensität reduziert",
        "recovery_reduced_fair": "Eingeschränkte aktuelle Erholung: Umfang reduziert",
        "active_unstructured_constraint": "Aktive Einschränkung erfordert persönliche Prüfung",
    }
    return {
        "configured": planning.profile.primary_sport is not None,
        "primary_sport": SPORT_LABELS.get(
            planning.profile.primary_sport or "", planning.profile.primary_sport
        ),
        "experience": EXPERIENCE_LABELS.get(
            planning.profile.experience_level or "", planning.profile.experience_level
        ),
        "experience_years": planning.profile.experience_years,
        "constraint_note": planning.profile.constraint_note,
        "constraint_until": planning.profile.constraint_until,
        "goal": (
            {
                "sport": SPORT_LABELS.get(goal.sport, goal.sport),
                "event_name": goal.event_name,
                "target_date": goal.target_date,
                "distance": format_distance(goal.distance_m) if goal.distance_m else None,
                "target_time": (
                    format_duration(goal.target_duration_s) if goal.target_duration_s else None
                ),
                "target_pace": (
                    format_pace(goal.target_pace_s_per_km) if goal.target_pace_s_per_km else None
                ),
            }
            if goal
            else None
        ),
        "availability": [
            {"day": WEEKDAY_LABELS[item.weekday], "minutes": item.max_duration_minutes}
            for item in planning.availability
        ],
        "performance": performance_items,
        "overview_performance": overview_performance,
        "zones": [
            {
                "sport": SPORT_LABELS.get(sport, sport),
                "type": "Herzfrequenz" if zone_type == "heart_rate" else "Leistung",
                "unit": "bpm" if zone_type == "heart_rate" else "W",
                "values": values,
            }
            for (sport, zone_type), values in sorted(zone_groups.items())
        ],
        "best_efforts": [
            {
                "distance": {
                    "1k": "1 km",
                    "5k": "5 km",
                    "10k": "10 km",
                    "half_marathon": "Halbmarathon",
                    "marathon": "Marathon",
                }[item.distance_key],
                "duration": format_duration(item.duration_s),
                "occurred_on": item.occurred_on,
                "source": (
                    "Garmin Personal Record"
                    if item.source == "garmin_personal_record"
                    else "Beobachteter Split"
                    if item.source == "observed_split"
                    else "Originale FIT-Aufzeichnung"
                    if item.source == "fit"
                    else "Garmin-Detaildaten"
                ),
                "activity_url": (
                    f"/activities/{item.activity_id}" if item.activity_id is not None else None
                ),
                "confidence": {
                    "reported": "Gemeldet",
                    "high": "Hoch",
                    "medium": "Mittel",
                }[item.confidence],
            }
            for item in automatic.best_efforts
        ],
        "weekly_capacity": {
            "covered": automatic.weekly_capacity.covered,
            "sustainable_distance": (
                format_distance(automatic.weekly_capacity.sustainable_distance_m)
                if automatic.weekly_capacity.sustainable_distance_m is not None
                else None
            ),
            "distance_range": (
                f"{format_distance(automatic.weekly_capacity.weekly_distance_p25_m)}–"
                f"{format_distance(automatic.weekly_capacity.weekly_distance_p75_m)}"
                if automatic.weekly_capacity.weekly_distance_p25_m is not None
                and automatic.weekly_capacity.weekly_distance_p75_m is not None
                else None
            ),
            "sessions": automatic.weekly_capacity.sessions_per_week_median,
            "active_days": automatic.weekly_capacity.active_days_per_week_median,
        },
        "longest_runs": [
            {
                "stratum": {
                    "road": "Straße",
                    "trail": "Trail",
                    "treadmill": "Laufband",
                }[item.stratum],
                "distance": format_distance(item.distance_m),
                "duration": format_duration(item.duration_s),
                "occurred_on": item.occurred_on,
                "activity_url": f"/activities/{item.activity_id}",
            }
            for item in automatic.longest_runs
        ],
        "intensity": {
            "sufficient": automatic.intensity.sufficient,
            "coverage": automatic.intensity.coverage_percent,
            "activities": automatic.intensity.eligible_activities,
            "low": automatic.intensity.low_percent,
            "moderate": automatic.intensity.moderate_percent,
            "high": automatic.intensity.high_percent,
        },
        "training_ranges": [
            {
                "key": item.key,
                "label": {
                    "easy": "Easy Run",
                    "tempo": "Tempolauf",
                    "interval": "Intervalle",
                    "long_run": "Long Run",
                }[item.key],
                "stratum": {
                    "road": "Straße",
                    "trail": "Trail",
                    "treadmill": "Laufband",
                }[item.stratum],
                "sufficient": item.sufficient,
                "pace": (
                    format_pace(item.pace_median_s_per_km)
                    if item.pace_median_s_per_km is not None
                    else None
                ),
                "pace_range": (
                    f"{format_pace(item.pace_p25_s_per_km)}–{format_pace(item.pace_p75_s_per_km)}"
                    if item.pace_p25_s_per_km is not None and item.pace_p75_s_per_km is not None
                    else None
                ),
                "heart_rate": (
                    f"{round(item.heart_rate_median)} bpm"
                    if item.heart_rate_median is not None
                    else None
                ),
                "sessions": item.sample_sessions,
                "efforts": item.sample_efforts,
                "minutes": item.sample_minutes,
            }
            for item in automatic.training_ranges
        ],
        "detail_evidence": {
            "formula_version": automatic.detail_evidence.formula_version,
            "coverage": {
                "eligible": automatic.detail_evidence.coverage.eligible_activities,
                "analyzed": automatic.detail_evidence.coverage.analyzed_activities,
                "fit": automatic.detail_evidence.coverage.fit_activities,
                "sampled": automatic.detail_evidence.coverage.sampled_detail_activities,
            },
            "drift": {
                "sufficient": automatic.detail_evidence.heart_rate_drift.sufficient,
                "percent": automatic.detail_evidence.heart_rate_drift.median_percent,
                "sessions": automatic.detail_evidence.heart_rate_drift.sample_sessions,
                "latest_on": automatic.detail_evidence.heart_rate_drift.latest_on,
                "confidence": {
                    "high": "Hoch",
                    "medium": "Mittel",
                    "insufficient": "Nicht ausreichend",
                }[automatic.detail_evidence.heart_rate_drift.confidence],
            },
            "threshold_segments": [
                {
                    "activity_url": f"/activities/{item.activity_id}",
                    "occurred_on": item.occurred_on,
                    "duration": format_duration(item.duration_s),
                    "pace": format_pace(item.pace_s_per_km),
                    "heart_rate": (
                        f"{round(item.heart_rate)} bpm" if item.heart_rate is not None else None
                    ),
                    "pace_cv": str(item.pace_cv_percent).replace(".", ","),
                    "elevation": (
                        f"+{item.elevation_gain_m:.0f}/-{item.elevation_loss_m:.0f} m"
                        if item.elevation_gain_m is not None and item.elevation_loss_m is not None
                        else None
                    ),
                    "source": "FIT" if item.source == "fit" else "Detaildaten",
                }
                for item in automatic.detail_evidence.threshold_segments
            ],
        },
        "performance_state": {
            "formula_version": state.formula_version,
            "endurance": {
                "covered": state.endurance_base.covered,
                "weekly_distance": (
                    format_distance(state.endurance_base.sustainable_weekly_distance_m)
                    if state.endurance_base.sustainable_weekly_distance_m is not None
                    else None
                ),
                "sessions": state.endurance_base.sessions_per_week,
                "active_days": state.endurance_base.active_days_per_week,
                "longest_run": (
                    format_distance(state.endurance_base.longest_run_distance_m)
                    if state.endurance_base.longest_run_distance_m is not None
                    else None
                ),
                "low_intensity": state.endurance_base.low_intensity_percent,
                "drift": state.endurance_base.heart_rate_drift_percent,
            },
            "load": {
                "covered": state.habitual_load.covered,
                "recent_distance": (
                    format_distance(state.habitual_load.recent_weekly_distance_m)
                    if state.habitual_load.recent_weekly_distance_m is not None
                    else None
                ),
                "habitual_distance": (
                    format_distance(state.habitual_load.habitual_weekly_distance_m)
                    if state.habitual_load.habitual_weekly_distance_m is not None
                    else None
                ),
                "recent_duration": (
                    format_duration(state.habitual_load.recent_weekly_duration_s)
                    if state.habitual_load.recent_weekly_duration_s is not None
                    else None
                ),
                "habitual_duration": (
                    format_duration(state.habitual_load.habitual_weekly_duration_s)
                    if state.habitual_load.habitual_weekly_duration_s is not None
                    else None
                ),
                "recent_sessions": state.habitual_load.recent_sessions_per_week,
                "habitual_sessions": state.habitual_load.habitual_sessions_per_week,
            },
            "tolerance": {
                "covered": state.training_tolerance.covered,
                "position": {
                    "below_usual": "unter dem gewohnten Bereich",
                    "usual": "im gewohnten Bereich",
                    "above_usual": "über dem gewohnten Bereich",
                    "unknown": "noch nicht einordenbar",
                }[state.training_tolerance.distance_position],
                "recent_distance": (
                    format_distance(state.training_tolerance.recent_weekly_distance_m)
                    if state.training_tolerance.recent_weekly_distance_m is not None
                    else None
                ),
                "usual_range": (
                    f"{format_distance(state.training_tolerance.habitual_distance_p25_m)}–"
                    f"{format_distance(state.training_tolerance.habitual_distance_p75_m)}"
                    if state.training_tolerance.habitual_distance_p25_m is not None
                    and state.training_tolerance.habitual_distance_p75_m is not None
                    else None
                ),
                "hard_sessions": state.training_tolerance.recent_hard_sessions_per_week,
                "strain_coverage": state.training_tolerance.strain_coverage_percent,
            },
            "trends": trends,
            "strengths": [finding_view(item) for item in state.strengths],
            "development_gaps": [finding_view(item) for item in state.development_gaps],
            "data_gaps": [finding_view(item) for item in state.data_gaps],
            "quality": {
                "level": {"high": "Hoch", "medium": "Mittel", "low": "Niedrig"}[
                    state.data_quality.level
                ],
                "covered_weeks": state.data_quality.covered_complete_weeks,
                "running_sessions": state.data_quality.running_sessions,
                "distance": state.data_quality.distance_coverage_percent,
                "duration": state.data_quality.duration_coverage_percent,
                "strain": state.data_quality.strain_coverage_percent,
                "splits": state.data_quality.split_coverage_percent,
                "details": state.data_quality.detail_coverage_percent,
            },
        },
        "planning_limits": {
            "schema_version": limits.schema_version,
            "formula_version": limits.formula_version,
            "status": {
                "usable": "Nutzbar",
                "limited": "Konservativ begrenzt",
                "review_required": "Prüfung erforderlich",
                "insufficient": "Daten nicht ausreichend",
                "unsupported": "Sportart nicht unterstützt",
            }[limits.status],
            "automatic_allowed": limits.automatic_generation_allowed,
            "week": (
                f"{limits.week_start.strftime('%d.%m.')}–{limits.week_end.strftime('%d.%m.%Y')}"
            ),
            "confidence": {
                "high": "Hoch",
                "medium": "Mittel",
                "low": "Niedrig",
                "insufficient": "Nicht ausreichend",
            }[limits.confidence],
            "volume": {
                "sustainable": (
                    format_distance(limits.weekly_volume.sustainable_distance_m)
                    if limits.weekly_volume.sustainable_distance_m is not None
                    else None
                ),
                "max": (
                    format_distance(limits.weekly_volume.week_distance_max_m)
                    if limits.weekly_volume.week_distance_max_m is not None
                    else None
                ),
                "progression": limits.weekly_volume.progression_max_percent,
                "available_duration": (
                    format_duration(limits.weekly_volume.scheduled_duration_max_s)
                    if limits.weekly_volume.scheduled_duration_max_s is not None
                    else None
                ),
            },
            "hard": {
                "max": limits.hard_sessions.max_sessions,
                "spacing": limits.hard_sessions.minimum_spacing_days,
                "avoid_long_run": limits.hard_sessions.avoid_adjacent_to_long_run,
            },
            "long_run": {
                "distance": (
                    format_distance(limits.long_run.distance_max_m)
                    if limits.long_run.distance_max_m is not None
                    else None
                ),
                "duration": (
                    format_duration(limits.long_run.duration_max_s)
                    if limits.long_run.duration_max_s is not None
                    else None
                ),
                "share": limits.long_run.weekly_share_max_percent,
            },
            "periodization": {
                "phase": {
                    "build": "Aufbau",
                    "deload": "Entlastung",
                    "taper": "Taper",
                    "event": "Wettkampfwoche",
                }[limits.periodization.phase],
                "multiplier": round(limits.periodization.volume_multiplier * 100),
                "block_week": limits.periodization.block_week,
                "taper_weeks": limits.periodization.taper_weeks,
            },
            "adjustments": [
                {
                    "label": adjustment_labels[reason.code],
                    "kind": item.kind,
                }
                for item in limits.adjustments
                for reason in item.reasons
            ],
            "targets": [
                {
                    "label": target_labels[item.key],
                    "pace": (
                        f"{format_pace(item.pace_fast_s_per_km)}–"
                        f"{format_pace(item.pace_slow_s_per_km)}"
                        if item.pace_fast_s_per_km is not None
                        and item.pace_slow_s_per_km is not None
                        else None
                    ),
                    "heart_rate": (
                        f"{round(item.heart_rate_min_bpm)}–{round(item.heart_rate_max_bpm)} bpm"
                        if item.heart_rate_min_bpm is not None
                        and item.heart_rate_max_bpm is not None
                        else None
                    ),
                    "heart_rate_zone": (
                        f"HF-Zone {item.heart_rate_zone}"
                        if item.heart_rate_zone is not None
                        else None
                    ),
                    "source": target_source_labels[item.source],
                }
                for item in limits.targets
            ],
        },
        "formula_version": automatic.formula_version,
        "capacity": [
            ("Einheiten · 28 Tage", str(planning.training_capacity.workouts_28d)),
            (
                "Laufumfang · 28 Tage",
                format_distance(planning.training_capacity.running_distance_28d_m),
            ),
            (
                "Trainingsdauer · 28 Tage",
                format_duration(planning.training_capacity.duration_28d_s),
            ),
            ("Harte Einheiten · 28 Tage", str(planning.training_capacity.hard_workouts_28d)),
        ],
        "history_complete": planning.training_capacity.history_complete,
        "warnings": planning.warnings,
    }
