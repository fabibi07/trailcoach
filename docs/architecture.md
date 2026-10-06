# TrailCoach Architecture

## Raw Data First

TrailCoach treats the original source payload as the ground truth for every
athlete record:

```text
Raw source data
      ↓
immutable / raw representation  (RawFile)
      ↓
source-specific representation  (SourceActivity, WellnessDaily per source)
      ↓
canonical athlete model         (Activity, AthleteStateSnapshot)
      ↓
derived metrics                 (streams, load, PMC, AI Coach context)
```

Principles:

1. **Raw files are immutable and content-addressable**.
   `RawFile` stores a SHA256 of the original payload; ingesting the same bytes
   twice returns the existing row and never overwrites the file on disk.
2. **Source-specific records are preserved** before canonicalization.
   `SourceActivity` and `WellnessDaily` keep the original source identifiers,
   raw values and provenance.
3. **Canonical models are built from sources, never written directly by a
   source adapter.**
4. **Multiple sources can coexist for the same athlete and date.**
   `WellnessDaily` is keyed by `(athlete_id, date, source)` so Garmin, Oura
   and WHOOP records can live side by side.
5. **Lineage is attached to every canonical object.**
   `provenance` JSON on `Activity` and `WellnessDaily` records the source,
   source record id, raw file, device and account.

6. **Per-source summary metrics stay per source.**
   `activity_source_metric` and `activity_zone_time` store provider-neutral
   metrics (`calories_kcal`, `aerobic_training_effect`, HR/power zone times)
   keyed by `source_activity_id`; `trailcoach reprocess-raw` rebuilds them
   from `RawFile` via `SourceProvider.payload_from_raw`.

This pipeline lets TrailCoach add, replace or reprocess individual source
adapters without losing the original data.

## Ingestion Pipeline

Providers (`SourceProvider`) are pure: they turn a source into detached
`SourceActivity`/`Activity` instances plus an optional `ActivityPayload`
(raw bytes, canonical stream rows, intervals, device identity). They never
open transactions or touch the filesystem beyond reading their input.

`IngestionOrchestrator` owns all side effects, for every source:

```text
provider.initial_import / incremental_sync
      ↓
DedupeEngine.evaluate            (REIMPORT / AUTO_LINK / REVIEW / SEPARATE)
      ↓
Device upsert  ·  RawStore.store  ·  MetricCapability upsert
      ↓
SourceActivity + Activity + ActivityLink (+ DedupeReview)
      ↓
streams parquet + intervals  ·  DataLineage provenance
      ↓
AthleteSourceAccount cursor / health / last_success_at
```

`GarminFitProvider` (`garmin_fit`) is the first real provider: it reads a
local directory of exported `.fit` files (`auth_json.import_dir`), with no
Garmin API or credentials. `source_activity_id` is `<serial>-<time_created>`
from the FIT `file_id` message (falls back to the SHA256 of the file), so
re-exporting the same activity is an idempotent `REIMPORT`.
