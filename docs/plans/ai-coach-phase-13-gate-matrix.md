# Phase 13 - Produktionshärtung - Gate-Matrix

**Stand:** 26. August 2026  
**Feature Flags:** globale Coach-Flags plus optionale interne Kohorte über
`COACH_ROLLOUT_USER_IDS`

| Gate | Automatisierte Evidenz | Erwarteter Zustand | Verbotene Nebenwirkung |
|---|---|---|---|
| Privacy-safe Decision Trace | `tests/test_production_hardening.py::test_decision_trace_and_metrics_exclude_sensitive_payloads` | Nur Versionen, Evidence-Referenzen, Regelcodes und boolesche Ergebnisse | Keine Workout-Texte, Health-Werte, Feedback-IDs oder Reports |
| Lifecycle-Metriken | `tests/test_production_hardening.py::test_metrics_endpoint_is_hidden_without_valid_bearer_token` | Proposal, Validation, Edit, Accept, Reject, Sync und Adaptation werden aus persistenten Ledgers aggregiert | Keine Nutzer-, Workout- oder Garmin-IDs in der Metrikantwort |
| Rate Limits | `tests/test_http_security.py::test_coach_rate_limit_runs_after_csrf_and_returns_retry_after` | Per-User-/Client-Limit mit `429` und `Retry-After` | Ungültiges CSRF verbraucht kein Nutzerkontingent |
| Security Headers | `tests/test_http_security.py::test_dynamic_responses_have_security_and_no_store_headers` | Dynamische Antworten sind `no-store`; Framing, MIME-Sniffing, Referrer und Browser-Berechtigungen sind begrenzt | Versionierte statische Assets verlieren ihre getrennte Cache-Policy nicht |
| Vollständiger Export | `tests/test_account_lifecycle.py::test_complete_export_is_user_scoped_and_excludes_tokens` | Alle 40 user-scoped Anwendungstabellen und Rohaktivitätsdateien liegen im ZIP | Keine Tokens, Sessions, Logs, Hostpfade oder Daten anderer Nutzer |
| Account-Löschung | `tests/test_account_lifecycle.py::test_account_deletion_removes_database_rows_files_and_tokens` | User-Root-Cascade, Rohdateien und lokale Garmin-Tokens werden entfernt | Kein Garmin-Remote-Erfolg wird behauptet oder vorausgesetzt |
| Garmin Contract Drift | `tests/test_production_hardening.py::test_calendar_contract_drift_stops_reconciliation` | Unerwartete Kalenderantwort stoppt mit `garmin.contract_drift` | Keine leere Antwort darf einen unbewiesenen Retry auslösen |
| Synthetische Contracts | `tests/test_production_hardening.py::test_synthetic_contract_fixtures_contain_no_sensitive_fields` | Garmin- und Coach-Fixtures sind synthetisch und versioniert | Keine Token-, GPS-, Cookie- oder OAuth-Felder |
| Prompt-/Mutation-Red-Team | `tests/test_production_hardening.py::test_prompt_injection_corpus_cannot_expand_coach_mutation_authority` | Der Coach besitzt nur ein begrenztes Proposal-Mutationstool | Keine Accept-, Schedule-, Push- oder Delete-Tools |
| Stuck/Unknown Garmin | `tests/test_scheduler.py::test_stale_pending_attempt_becomes_unknown_while_process_is_running` | Alte Pending Attempts werden periodisch `unknown` und sichtbar | Kein automatischer Retry bei unklarem Remote-Ausgang |
| Rollout/Rollback | `tests/test_config.py::test_coach_rollout_allowlist_fails_closed` und bestehende Feature-Flag-Tests | Globale Kill-Switches und interne Nutzerkohorte wirken serverseitig | Fehlkonfiguration öffnet keine Funktionen; persistierte Daten bleiben erhalten |
| CI | `.github/workflows/docker-publish.yml` | Python-Gates, reproduzierbares CSS und PR-Image-Build müssen bestehen | Kein Image-Publish vor grünen Gates |

## Commands

```text
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
npm ci
npm run build:css
git diff --check
```

## Browser Waiver

Der Masterplan nennt einen Browser-E2E- und Accessibility-Pass. Der Nutzer hat Browserautomation
für dieses Projekt ausdrücklich ausgeschlossen. Deshalb bleibt diese Prüfung wie in Phase 11B und
12 als dokumentiertes visuelles Restrisiko bestehen; serverseitiges Rendering, CSRF-Abdeckung,
semantische Markup-Checks und responsive Klassen bleiben automatisiert getestet.
