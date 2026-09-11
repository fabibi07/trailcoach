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

This pipeline lets TrailCoach add, replace or reprocess individual source
adapters without losing the original data.
