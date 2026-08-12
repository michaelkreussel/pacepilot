"""Deterministic athlete analytics for UI and future coach consumers."""

from app.services.analytics.athlete_data import AthleteDataService
from app.services.analytics.automatic_profile import AutomaticAthleteProfile

__all__ = ["AthleteDataService", "AutomaticAthleteProfile"]
