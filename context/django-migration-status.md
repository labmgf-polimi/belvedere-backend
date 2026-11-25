# Django Migration Status

## Goal (ACHIEVED — June 2026)

The project/database is now in a fully Django-managed state:
- models are the authoritative schema definition
- all tables live in a single `public` schema
- schema changes (tables **and views**) happen through Django migrations
- the full migration chain builds a complete database from scratch (validated by the test suite)

## What was done (migrations `surveys/0008`–`0014`, applied June 2026)

### 0008 — un-shadow `points` / `surveys`
The legacy views `public.points` (aggregate) and `public.surveys` shadowed the
real `core.points`/`core.surveys` tables via the `public,core` search_path, so
Django models read views instead of tables — and `Point` admin saves failed
(aggregate views are not updatable). Fixed by renaming `public.points` →
`points_summary`, dropping `public.surveys`, and removing the three
view-computed fields (`first_survey_date`, `last_survey_date`,
`num_measurements`) from the `Point` model. `PointAdmin` now computes them
with queryset annotations.

### 0009 — single schema
All 12 `core` tables moved to `public` (`ALTER TABLE ... SET SCHEMA`), the
`compute_measurement_geometry()` trigger function moved with them, the empty
`core` schema dropped. `settings.py` search_path is now just `public`.

### 0010 — products & volumes modelled
Legacy pandas/shapefile tables `2d_products`, `3d_products`, `volumes`
replaced by clean Django models `Product2D` (`products_2d`), `Product3D`
(`products_3d`), `Volume` (`volumes`); data copied, truncated column names
expanded, all-NULL `average_no` dropped. Products carry S3 reference fields
(`bucket`, `object_key`, etag/size/last-modified, `is_uploaded`) mirroring
`image_index.Image`; `S3_PRODUCTS_BUCKET_NAME` setting (default
`belvedere-products`, bucket not yet created) and the `index_s3_products`
management command provide the upload-indexing infrastructure.

### 0011 — views under migration control
Surviving views adopted into migrations (DDL now version-controlled, created
on fresh DBs): `points_measurements`, `points_movement_raw`,
`points_movement_filtered`, `active_points`. Dropped as legacy:
`points_summary` (0011), plus manually by the user: `measurements_2015..2023`
per-year views, `scatter_points_measurements`, `scatter_points_movement`.

**Rule going forward:** any change to these views must be a new migration
(`CREATE OR REPLACE VIEW` + reverse), never manual DDL in pgAdmin.

### 0012–0014 — admin polish & views in admin
Meta/verbose-name tweaks (state-only) and unmanaged models
(`managed = False`) over the four kept views: `PointsMeasurement`,
`PointsMovementRaw`, `PointsMovementFiltered`, `ActivePoint` — exposed in the
Django admin as browse-only pages (`ReadOnlyViewAdmin`).

## Surveys API (June 2026)

REST endpoints under `/surveys/` now serve the data the legacy PHP viewers
used to read straight from the views (`surveys/urls.py`, `views.py`,
`serializers.py`):

- `GET /surveys/years/` — distinct survey years with measurements
- `GET /surveys/measurements/?year=YYYY&is_fixed=false` — plain JSON array,
  field names identical to the `points_measurements` view
- `GET /surveys/points/<label>/velocity/` — yearly velocity series. Since
  `surveys/0031` it reads the `point_movements` view (horizontal, m/day);
  `surveys/utils/velocity.py` was removed

The DB views therefore remain only for QGIS; external consumers should use
the API.

## Local ENU reference frame (August 2026)

Migrations `georef/0001–0003`, `surveys/0020`, `image_index/0009` add the
glacier-local topocentric ENU frame, SRID 990001. See `architecture.md` for the
design; the notes that matter for migrations:

- `georef/0001` — `ReferenceFrame`, with `proj_pipeline` as a Postgres
  **generated column**. It uses `||` and `::text`, not `concat()`: `concat()` is
  STABLE and Postgres rejects non-immutable expressions in generated columns.
- `georef/0002` — `georef_to_enu` / `georef_from_enu` and the freeze trigger.
- `georef/0003` — data migration: inserts the frame row (lat_0/lon_0 *derived*
  with PostGIS from the frozen UTM origin, not transcribed) and the
  `spatial_ref_sys` row. That is **DML on a PostGIS-owned table**, not DDL — the
  rule against touching PostGIS relations still holds.
- `surveys/0020`, `image_index/0009` — the materialised `geom_enu` /
  `location_enu` columns, their triggers, and the backfills. Both depend on
  `georef/0003`.
- `georef/0004` — sets `z_off = 1000` (was 0, which left every point below D12
  with a negative U). Rewrites the `spatial_ref_sys` srtext, whose REMARK embeds
  the pipeline, and recomputes both materialised columns. E/N are unaffected:
  moving the origin along the ellipsoid normal is a pure U translation.

`georef/sql.py` holds the statements that *derive* state from the frame — the
`spatial_ref_sys` upsert and the two recompute statements — because 0004 and
`recompute_enu` both regenerate them and they must not drift. The already-applied
0003/0020/0009 keep their own copies as a record of what ran; on a fresh database
0004 sets the final state anyway.

**Rule going forward:** a frozen frame is never updated. Corrections mean a new
row with a new SRID and a new geometry column.

## Datum correction: 32632 → 7791 (August 2026)

Migrations `surveys/0022–0023`, `image_index/0010`, `georef/0006` re-tag every
geometry as **EPSG:7791** (RDN2008 / UTM 32N). See `architecture.md` for why.

- `surveys/0022` — `SeparateDatabaseAndState` is **required**: Django's own
  `AlterField` emits `USING geom::geometry(POINT,7791)`, which PostGIS rejects
  because the cast does not re-tag the SRID. The explicit DDL uses
  `USING ST_SetSRID(geom, 7791)` — a re-tag, not a re-projection.
  The four views must be dropped first (Postgres refuses `ALTER COLUMN TYPE`
  while a view depends on the column) and recreated after; their DDL now lives
  in `surveys/sql.py` so it has a single home.
- **`compute_measurement_geometry()` is now migration-managed**, and gained two
  fixes it needed anyway: it samples the geoid in the *raster's* SRID (the
  raster is 32632 while measurements are 7791 — mixing them would fail every
  insert) and skips the geoid when `raster.geoid_model` is absent, which is the
  case on test databases.
- `surveys/0023` — `datum_realization` and `height_type` on `Measurement`,
  backfilled `RDN2008` / `ellipsoidal`. Deliberately only two columns: the SRID
  is already on `geom`, and an observation epoch would be NULL on every row
  because RDN2008 is plate-fixed.
- `georef/0006` — moves the ENU pipeline onto EPSG:6705, re-derives the frame
  origin as RDN2008, sets `base_srid`/`datum_epoch`, regenerates
  `spatial_ref_sys` and recomputes both materialised columns. ENU coordinates
  move ~0.12 mm.

### Rolling back

Nothing here overwrites existing data — `east`/`north`/`h`, `geom`, `location`
and every legacy table are untouched. The change is purely additive, so the
migration reverse is the first resort and the dump is only for disasters.

**1. Reverse the migrations** (preferred — no data loss):

```bash
uv run python manage.py migrate georef zero
```

One command is enough: Django pulls in the dependants automatically and
unapplies in the right order — `georef.0004` → `surveys.0020` →
`image_index.0009` → `georef.0003` → `0002` → `0001`. It drops the
`geom_enu`/`location_enu`
columns and their triggers, restores `image_index_camera_sync_location()` to
its 0007 body, deletes the frame row and the `spatial_ref_sys` 990001 row, and
drops the `georef_*` functions and table. A frozen frame does not block this:
the 0003 reverse clears `frozen` before deleting.

Re-applying is just `manage.py migrate` again — the backfills are idempotent.

To undo only the height offset, keeping the frame and its columns:

```bash
uv run python manage.py migrate georef 0003_belvedere_frame
```

That restores `z_off = 0`, refreshes `spatial_ref_sys`, and recomputes both
materialised columns. It refuses if the frame is already frozen.

**2. Restore from the dump** (only if the schema is actually damaged):

```bash
pg_restore -h $DB_HOST -p $DB_PORT -U $DB_USER -d belvedere \
    --clean --if-exists --no-owner --no-privileges \
    db-backups/belvedere_20260805_090017.dump
```

Caveats: this **discards everything written since the dump was taken**; every
QGIS client must disconnect first (`--clean` needs to drop objects); and
`spatial_ref_sys` is owned by `postgres`, so expect permission warnings on
that table when restoring as `belvedere` — they are harmless, PostGIS
re-creates it. Restoring into a fresh database and renaming is the safer
variant when the live DB is still serving.

**Verifying either path:**

```sql
SELECT count(*) FROM measurements WHERE geom_enu IS NULL;  -- 0 after apply
SELECT to_regclass('georef_reference_frame');              -- NULL after reverse
```
plus `manage.py validate_reference_frame` (read-only) and
`manage.py showmigrations georef surveys image_index`.

## Intentionally outside Django

- `scatter_points`, `scatter_measurements` — legacy tables kept for reference
  only (see comment block at the bottom of `surveys/models.py`)
- `layer_styles`, `qgis_projects` — owned by QGIS
- `spatial_ref_sys` — PostGIS
- `raster.geoid_model` — geoid raster sampled by the
  `compute_measurement_geometry` BEFORE INSERT/UPDATE trigger on
  `measurements` (computes `geom`, `lat`, `lon`, `h_orto`)
