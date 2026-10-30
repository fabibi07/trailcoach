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

### `GarminConnectProvider` (`garmin_connect`, P0.2)

Proveedor de la API Personal oficial de Garmin Connect (solo *pull*). Es una
fuente distinta de `garmin_fit`: la API entrega el **mismo FIT original** del
reloj (verificado byte a byte), que se parsea con el mismo `FitParser`.

- **Actividades:** se listan en páginas de 100, se descarga el ZIP de cada FIT
  (`Accept: */*`) y se construye el mismo `ActivityPayload` que `garmin_fit`
  (`fit_payload`). `source_activity_id` es el `activityId` de Garmin. Contra
  un export local la deduplicación produce `AUTO_LINK`, y el `RawStore`
  reutiliza el `RawFile` existente porque el SHA-256 es idéntico.
- **Bienestar:** resumen diario (≤365 días por llamada), sueño (≤31 días) y
  HRV (≤365 días). Cada respuesta JSON es un `WellnessPayload`: el
  orquestador guarda el RAW (`kind="wellness_json"`) y enlaza a él los días
  (`WellnessDaily`, columnas canónicas) y las métricas extra
  (`WellnessSourceMetric`: pasos, estrés, etapas de sueño, línea base HRV...).
  Los campos `None` no borran valores ya guardados; el día en curso queda
  `data_quality="partial"` con `day_in_progress`.
- **Credencial:** la API key nunca se guarda en la base de datos;
  `auth_json` solo contiene `{"key_env": "GARMIN_PERSONAL_API_KEY"}`. El JWT
  (~24 h) vive en memoria. HTTP 401/403 → `auth_status="expired_or_revoked"`
  y `health="error"`.
- **No disponible en la API:** Training Readiness, Training Status, historial
  de VO2max, predicciones de carrera y Health Snapshots.
- CLI: `trailcoach ingest-garmin-connect --athlete-id ... [--from-date
  --to-date] [--incremental] [--no-wellness]`.

## Umbrales del atleta (P0.3)

- `threshold_estimate` guarda **todos** los candidatos mensuales de cada umbral
  (`kind`, `valid_from`, `value`, `method`, `source`, `confidence`, `evidence`).
- `athlete_threshold` guarda solo el valor efectivo; `source` = `manual`, `derived`
  o `device` y `notes` = `method` del candidato elegido (trazabilidad).
- `training/threshold_estimation.py`: funciones puras sobre streams, ajustes
  `device_*` neutrales (`activity_source_metric`) y `WellnessDaily.resting_hr`
  (fuente según `SourcePriority`). Sin lógica por proveedor.
- Prioridad: manual > (derivado | dispositivo según `PREFERENCE`). LTHR prefiere
  el dispositivo; el derivado (mejor 20 min × 0,95) solo genera avisos.
- Guarda de pulso (30–220 ppm, saltos > 8 ppm/s) solo para estimar; los streams
  no se modifican. Versionado mensual solo si cambia ≥ 2 ppm o ≥ 2 %.
- `trailcoach estimate-thresholds --athlete-id ... [--apply]` es idempotente:
  reemplaza solo filas `derived`/`device`; un manual gana desde su `valid_from`.

## Ritmo umbral y sincronización diaria (P0.4)

- **Ritmo umbral (`ftp_pace_mps`):** preferencia `lthr_segments_90d` (tramos ≥ 8 min a LTHR ± 3, D5)
  y, si no hay 3 tramos, `gap_20min_x0.95_90d`: mejor media de 20 min del ritmo ajustado por
  pendiente (GAP) en 90 días × 0,95, con al menos 3 carreras al aire libre (sin cinta/indoor).
- **Guarda de velocidad:** se descartan, solo para estimar, velocidades > 7 m/s y aceleraciones
  > 3 m/s². Los streams no se modifican.
- **FTP de potencia (`ftp_power_w`):** el derivado `power_20min_x0.95_90d` gana al ajuste del
  dispositivo; ambos quedan en `threshold_estimate`.
- **Carga principal:** ruta/pista usa rTSS; trail usa HRSS (rTSS queda como métrica secundaria).
- **Test manual de 30 min:** calentar, correr 30 min al máximo esfuerzo sostenible en terreno
  plano, tomar el ritmo medio de los últimos 20 min y cargarlo con
  `trailcoach set-threshold --kind ftp_pace_mps --value <m/s> --source manual`. El manual gana
  siempre.
- **Sincronización diaria:** `trailcoach sync-daily --athlete-id ...` encadena
  `ingest-garmin-connect --incremental`, `estimate-thresholds --apply` y `recalculate`. Se programa
  una vez al día (cron o Programador de tareas de Windows).
