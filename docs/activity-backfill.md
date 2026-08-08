# Historical Activity Backfill

## Behavior

`app.services.garmin.activity_backfill.sync_activity_history()` imports the complete Garmin activity
list for each connected user. The initial import uses offset pages and stores its next page in the
user's `garmin_sync_states` row. Each completed activity is committed independently, so a failed page
can be retried without downloading detail for activities already completed on that page.

The normal Garmin sync checks the most recent page. A SHA-256 fingerprint contains only stable,
coaching-relevant Garmin summary fields. If the fingerprint is unchanged, detail and split data are
complete, and the raw files still exist, the activity is skipped. New or edited activities are
upserted and their detail children are replaced.

The command-line entry point processes every connected account unless one is selected:

```powershell
uv run python scripts/backfill_garmin_activities.py
uv run python scripts/backfill_garmin_activities.py --account-id 1
```

## Stored Data

Activity summaries include:

- Garmin activity ID, sport, timestamps, distance, duration/elapsed/moving time, speed, calories,
  elevation, and HR;
- cadence, power, normalized power, VO2max, running dynamics, and intensity minutes when supplied;
- aerobic/anaerobic Training Effect, Exercise Load when supplied, Body Battery change, perceived
  effort, and workout feel;
- Garmin's associated workout ID and an optional link to the matching local planned workout.

Normalized children include:

- HR and power zone boundaries plus seconds in zone;
- Garmin laps;
- typed run/walk/stand and workout-interval splits;
- strength sets with duration, repetitions, weight, exercise category/name, and workout-step index.

Sampled chart detail remains compressed JSON because it is larger and is already consumed by the
activity-detail UI. Raw activity summaries and details are isolated under
`data/raw/activities/user-<user id>/<year>/`.

GPS samples remain inside Garmin's compressed sampled detail used by the existing map; they are not
normalized into SQLite or used for historical trend aggregation.

## Configured Account Verification

The live backfill completed without authentication or rate-limit failures.

| Item | Result |
|---|---:|
| Garmin activities | 62 |
| Local activity range | 2026-03-30 to 2026-08-06 |
| Inserted | 56 |
| Existing activities upgraded | 6 |
| Initial API calls | 373 |
| Complete raw summary files | 62 |
| Complete sampled detail files | 62 |
| Normalized splits | 828 |
| Normalized zones | 450 |
| Strength exercise sets | 43 |

The account contains 33 strength activities, 27 runs, one hike, and one trail run. All 62 activities
have Training Effect. Power, VO2max, perceived effort, and running dynamics are available on 28
activities. Exercise Load is absent from every account payload and remains null.

The oldest strength activity still has sampled detail, one lap, five HR zones, five strength sets,
and Training Effect. The recent run has sampled detail, 54 lap/typed-split rows, HR and power zones,
power, VO2max, perceived effort, and running dynamics.

An immediate rerun made two API calls (`count_activities` and the recent activity page), reported all
62 activities unchanged, wrote no detail files, and left all database counts unchanged.

The account history covers only one watch era and about four months. Differences between the oldest
strength activity and recent run are primarily sport/device metric availability, not demonstrated
historical field expiration.
