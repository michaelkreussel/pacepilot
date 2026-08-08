# Historical Health Backfill

## Behavior

`app.services.garmin.health_backfill.sync_health_history()` synchronizes each Garmin resource with
an independent user-scoped cursor. The first sync progressively discovers that resource's earliest
populated date, imports forward, and commits after each day or safe Body Battery range chunk.

Subsequent syncs fetch only dates after the newest covered date plus a configurable recent overlap.
The default `HEALTH_SYNC_OVERLAP_DAYS=7` allows Garmin to revise recent sleep, HRV, Body Battery, and
fitness values without repeating the historical import.

The synchronized resources are:

- daily Garmin summary, including steps, calories, HR, stress, respiration, and intensity minutes;
- Body Battery in at most 31-day requests;
- sleep totals, score components, sleep need, recovery-related values, and normalized stage
  intervals;
- nightly HRV, status, and Garmin baseline;
- SpO2 when recorded;
- VO2max;
- Garmin Training Readiness/recovery time when supported;
- Garmin Training Status/load when supported.

Successful no-data responses remain null and receive an `empty` completeness state. Unsupported
endpoints, authentication failures, rate limiting, and other API failures are recorded separately.
An empty or unsupported resource is re-probed after 28 days so a new watch or changed sensor setting
can add it later without creating hourly empty requests.

## Multi-account Isolation

Every data row and cursor is scoped by `user_id`. Garmin tokens are stored under
`GARMIN_TOKEN_DIR/account-<account id>/`. The first existing connected account can adopt the legacy
root `garmin_tokens.json` once; newly connected accounts write directly to their own directory.

The HTTP application still needs authentication and user selection before it is safe as a public
multi-user service. The synchronization and persistence layer no longer assumes that all connected
accounts share one Garmin token.

## Resume And Idempotency

`garmin_sync_states.backfill_cursor_date` points to the next uncommitted historical date. If an API
call fails, all earlier committed days remain complete and the next run continues at that cursor.

Daily values are upserted by `(user_id, day)`. Sleep-stage intervals are replaced as one complete
nightly snapshot. Per-resource/day statuses are upserted, so rerunning a range updates values without
creating duplicate health, fitness, stage, or status rows.

The command-line entry point processes all connected accounts unless an account is selected:

```powershell
uv run python scripts/backfill_garmin_health.py
uv run python scripts/backfill_garmin_health.py --account-id 1 --overlap-days 7
```

The normal Garmin synchronization also invokes the same service. A newly connected account receives
its historical import; an established account receives only new dates and overlap updates.

## Configured Account Verification

The initial live import completed without authentication or rate-limit failures.

| Resource | Local range | Populated records | Valid empty days |
|---|---:|---:|---:|
| Daily summary | 2026-03-26 to 2026-08-08 | 136 | 0 |
| Body Battery | 2026-03-27 to 2026-08-08 | 135 | 0 |
| Sleep | 2026-03-28 to 2026-08-08 | 119 | 15 |
| HRV endpoint | 2026-03-26 to 2026-08-08 | 129 useful responses | 7 |
| SpO2 | No recorded values | 0 | Resource-level empty |
| VO2max | 2026-07-01 to 2026-08-06 | 7 | 32 covered days |
| Garmin Training Readiness | No device support | 0 | Resource-level empty |
| Garmin Training Status/load | No populated values | 0 | Resource-level empty |

Local storage after import contained 136 daily-health rows, 39 sparse daily-fitness rows, 1,804
sleep-stage intervals, and 580 daily completeness records.

A representative night had exact agreement between interval totals and Garmin's aggregate stage
totals for Deep, Light, REM, and Awake. Sleep need was converted from Garmin minutes to local seconds;
sleep and stage durations were already seconds and were not converted.

The first ordinary rerun made 29 API calls for seven overlap days. Counts for health rows, fitness
rows, sleep stages, and completeness records remained unchanged, while recent values were upserted.
