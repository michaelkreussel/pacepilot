# AI Coach Phase 6 Gate Matrix

Phase 6 adds explicit subjective feedback and deterministic safety triage. The triage runs before
workout acceptance and before Garmin sync; it does not diagnose, generate workouts, or extract
facts from chat text.

| Gate | Test or check | Expected result |
|---|---|---|
| Four deterministic outcomes | `tests/test_feedback.py::test_triage_covers_allow_warn_clarify_and_safety_stop` | Identical structured input produces `allow`, `warn`, `clarify`, or `safety_stop` with stable issue codes |
| Gait-changing pain | `test_triage_covers_allow_warn_clarify_and_safety_stop` | Pain that changes gait produces a blocking `safety_stop` |
| Fever and systemic illness | `test_red_flags_stop_without_diagnostic_copy` | Fever and systemic illness produce a blocking stop with non-diagnostic German escalation copy |
| Cardiopulmonary warning | `test_red_flags_stop_without_diagnostic_copy` | Structured warning signs produce a blocking stop and appropriate professional escalation |
| Ambiguous pain | `test_triage_covers_allow_warn_clarify_and_safety_stop` | Incomplete "Knie komisch"-equivalent structured input requests location, severity, and gait clarification |
| Difficult completed session | `test_post_session_pain_and_difficult_session_are_deterministic` | High RPE, low completion/feel, or non-gait-changing pain produces conservative warnings |
| Wearable precedence | `test_positive_wearable_readiness_cannot_override_safety_stop` | Garmin readiness 100 cannot change a subjective safety stop |
| No diagnosis | `test_red_flags_stop_without_diagnostic_copy` | Stored and displayed rule copy names warning signals and actions, not a diagnosis |
| Stale browser acceptance | `test_new_safety_feedback_invalidates_acceptance_and_delete_does_not_reuse_cache` | Newer feedback changes the context fingerprint and rejects the older acceptance command |
| Failed evidence persists | `test_new_safety_feedback_invalidates_acceptance_and_delete_does_not_reuse_cache` | A blocking validation run is committed and visible from a new database session |
| Same-day delayed sync | `test_same_day_safety_stop_blocks_delayed_garmin_sync` | A newer stop blocks Garmin before account or network access |
| Future workout policy | `test_future_sync_does_not_require_unrelated_daily_feedback` | Future sync does not require unrelated daily feedback; acceptance still considers fresh athlete safety context |
| Feedback freshness | `test_feedback_expires_after_versioned_freshness_window` | Feedback older than seven days no longer contributes to a context or permanent stop |
| Export and deletion | `test_post_feedback_export_survives_garmin_activity_deletion_and_can_be_deleted` | User-authored feedback survives imported-activity deletion, exports explicitly, and can be hard-deleted |
| Derived-data deletion | `test_new_safety_feedback_invalidates_acceptance_and_delete_does_not_reuse_cache` | Deleting feedback removes validation reports that reference it and prevents cached-context reuse |
| User isolation | `test_feedback_service_is_user_scoped` and `test_subjective_feedback_migration_enforces_privacy_links` | Services and database constraints reject cross-user access and cross-user workout/activity links |
| Explicit German UI | `test_german_feedback_forms_routes_and_export` | Workout pages expose only optional progressive safety details; activity pages use Garmin or two direct manual fallback values with smileys; stops, deletion, and no-store export remain visible |
| Schema compatibility | `tests/test_migrations.py` | Fresh and previously applied revision-23 upgrades reach `20260824_24`; Alembic metadata check and FK behavior pass |

## Versioned Policies

- Rule set: `safety-triage-v1`.
- Freshness: seven rolling 24-hour periods, evaluated against naive UTC timestamps used by the
  application database.
- Acceptance considers all fresh pre- and post-session feedback for the athlete. This prevents a
  different workout or imported activity from hiding a current red flag.
- Same-day sync uses the same fresh athlete context. A future workout uses only fresh feedback
  explicitly linked to that workout, so daily subjective input is not mandatory for future plans.
- Severity precedence is `safety_stop > clarify > warn > allow`. Positive readiness, motivation,
  freshness, or Garmin metrics are not inputs that can reduce a safety severity.
- `safety_stop` and `clarify` are invalid contextual validations. `warn` remains valid but visible.
- Fever, systemic illness, cardiopulmonary warning signals, and gait-changing pain stop running.
  Ambiguous pain or illness requests clarification instead of guessing.
- Garmin RPE/feel and PacePilot post-session feedback remain separate sources. No values are copied
  between them. A field-level read model uses Garmin first and manual feedback only when Garmin is
  missing.
- The full structured safety schema is not a routine questionnaire. Current subjective text is
  stored locally only in the coach conversation, Health signals are available through read-only
  coach tools, and pain or illness details are requested only when relevant. No separate
  daily-check-in record is created.
- Explicit form input is authoritative. Phase 6 performs no LLM or keyword extraction from notes.
- Feedback is user-authored PacePilot data. Garmin-data deletion detaches an activity reference but
  preserves the feedback. Account erasure cascades it. Individual deletion hard-deletes the source
  and any contextual validation evidence that identifies it, then expires remaining cached runs.
- Export responses include structured values, links, timestamps, and optional text; they use
  `Cache-Control: no-store`.
