# Architecture

Backend for the **Belvedere Glacier monitoring project**: GNSS/photogrammetric
survey data and time-lapse camera imagery, served to external viewers
(potree 3D, web-map, camera-viewer in the `belvedere-viewers` repo) and to
QGIS (direct DB access).

One PostgreSQL/PostGIS database (`belvedere`), single `public` schema, fully
Django-managed (see `django-migration-status.md`). Never split into separate
databases.

## Coordinate reference systems

**Project CRS: EPSG:7791** — RDN2008 / UTM zone 32N, the Italian realization of
ETRF2000 at epoch 2008.0, on GRS80 with E-N axis order. Everything measured
against the Italian permanent network lands here.

It is deliberately **not EPSG:32632**, which the database used until August
2026. That code names the WGS 84 datum *ensemble*, whose declared accuracy is
2 m — a meaningless claim over centimetre GNSS. The coordinates are byte-for-byte
the same either way (PROJ treats RDN2008 → WGS 84 as a null transform, so the
correction moved the ENU results by 0.12 mm, purely the GRS80/WGS84 ellipsoid
difference). The label matters downstream: WGS 84 ≈ ITRF has drifted ~45 cm from
ETRF2000 since 2008.0, and a realization mismatch between campaigns would
masquerade as glacier motion. `measurements.datum_realization` and `height_type`
record the realization per row so any future exception is visible.

Companion codes, all RDN2008: **6705** geographic 3D (what the ENU pipeline
consumes), **6706** geographic 2D. Use `georef.constants.PROJECT_SRID` in code,
never a literal.

`scatter_points` / `scatter_measurements` remain on 32632 — legacy, kept for
reference, outside Django.

## Field metadata in QGIS

Field descriptions live in **Postgres column comments**, which QGIS shows in Layer
Properties → Fields and as attribute-form tooltips. They come from `db_comment`
on the model fields and `Meta.db_table_comment`, so they stay migration-managed.

**Views do not inherit column comments from base tables, and dropping a view drops
its comments.** Since the QGIS layers *are* the views, `surveys/sql.py` emits
`COMMENT ON COLUMN` for each view immediately after its `CREATE OR REPLACE VIEW`,
inside `create_views()`. Any future view rebuild therefore keeps its metadata —
never add view DDL that bypasses that function.

`COLUMN_COMMENTS` in `surveys/sql.py` is the single source for both the table and
the view wording, so a column cannot be described one way on `measurements` and
another way on `points_measurements`.

**`ds_*` → `std_*` (August 2026).** They are standard deviations in metres, but the
name invited confusion with the displacements in `points_movement_*`. Renamed in
`surveys/0024`, which drops and recreates the views — `CREATE OR REPLACE VIEW`
cannot rename an output column. This **broke the `/surveys/measurements/` response**
(`std_east` etc.); the web-map was updated in the same release, so backend and
website must deploy together. CSV import still accepts `ds_*` headers.

`surveys/0025` cleared the 2026 standard deviations: that campaign was entered with
displacements (30 of its 31 non-null rows were negative, impossible for a sigma).
2015–2025 are clean.

## Point labels are unique case-insensitively

`points.label` had a unique index, but a **case-sensitive** one, and
`MeasurementResource` looked points up by exact label. The 2026 CSV import
therefore created `D01BIS` beside the existing `D01bis` for 12 stakes. That is
not merely untidy: `points_movement_*` partitions by label, so each orphaned
2026 measurement got `dt = 0` and no displacement at all.

Fixed in August 2026 — `surveys/0027` adds `UniqueConstraint(Lower(Trim("label")))`,
and the importer now matches with `label__iexact`. `manage.py merge_duplicate_points`
(dry-run by default, `--apply` to write) finds groups sharing `lower(trim(label))`,
keeps the row with the most measurements (tie-break lowest id), re-points the
others' measurements onto it and deletes them. It refuses if both sides measure
the same survey.

`D38BIS` was **not** a duplicate — a genuinely new stake replacing `D38` — and was
only renamed to `D38bis` to match the lowercase-suffix convention. Replacement
stakes stay separate series in the movement views.

## Apps

### `georef` — local reference frames

Owns the glacier-local topocentric **ENU frame, SRID 990001** (`belvedere-enu`),
an exact rigid motion of ECEF: true distances and angles, no grid scale factor,
no convergence, 5-digit positive coordinates. Origin on GNSS monument **D12**
(UTM32N E 416125.449, N 5089423.209, h 2121.172 ellipsoidal), false origin
**+10000 / +10000 / +1000**. Data extent in the frame: E 9198–10511,
N 8555–11905, U 711–1171 — all positive.

**Z is a neutral offset, not an altitude.** `z_off = 1000` was chosen precisely
so U cannot be mistaken for a height: U is height above the tangent plane at
D12, plus 1000. An altitude-like offset (`z_off = h_0`) was rejected because U
would then read ≈1832–2292 while sitting up to 0.295 m below the true
ellipsoidal height (the `d²/2R` term) — a number that looks like an altitude
without being one. Exact heights live in `measurements.h` (ellipsoidal) and
`h_orto` (orthometric; geoid undulation ≈ 54.39 m here). The 1000 m of headroom
keeps U positive down to ~1121 m ellipsoidal, so extending the network
down-valley cannot reintroduce negative values.

U can never equal the altitude exactly: ENU's U axis is a straight line (the
ellipsoid normal at D12) while ellipsoidal height follows a curved family of
normals. Forcing them equal would break the orthonormality that makes distances
true. The gap is a function of horizontal position only, so it cancels exactly
in any time difference — velocities and elevation changes are unaffected.

- `ReferenceFrame` (`georef_reference_frame`) — structured parameters are
  canonical; `proj_pipeline` is a **generated column** built from them, so the
  string can never drift (Postgres `concat()` is STABLE, so it uses `||`).
  A DB trigger refuses to change or delete a `frozen` row: corrections mean a
  **new SRID**, never an UPDATE.
- SQL functions `georef_to_enu(geom, srid)` / `georef_from_enu(geom, srid)` —
  the sanctioned conversions, driven by `proj_pipeline` via
  `ST_TransformPipeline`. Pipeline is geographic-first (EPSG:4979 → `+proj=cart`
  → `+proj=topocentric` → `+proj=affine`), so the frame is not tied to UTM.
- `georef/enu.py` — thin **pyproj** wrappers (`to_enu`, `from_enu`,
  `transformer_for`) plus the two radius-of-curvature formulas the checks assert
  against. No hand-rolled geodesy: PostGIS is the production path, and this is
  for validation and offline consumers (Metashape/Blender exports).
- `georef/crs.py` — generates the `spatial_ref_sys` `proj4text`/`srtext` with
  pyproj. The caveat below is injected as the CRS's PROJJSON `remarks`, so it
  reaches QGIS as a WKT `REMARK[...]` node rather than living only in docs.
  The proj4 string is built by hand because `CRS.to_proj4()` warns and rounds
  the origin to 13 significant figures.
- `georef/examples/` — worked forward/inverse examples for `cct`, SQL, pyproj
  and Django (`README.md`), plus a standalone `enu_transform.py` that needs only
  pyproj so it can be dropped into a Metashape or Blender script. The test suite
  asserts its hardcoded pipeline still matches the database.
- `georef/validation.py` — the 12-check suite shared by the tests and
  `validate_reference_frame` / `freeze_reference_frame`. Sample points come from
  a `generate_series` grid built in SQL. Three checks are worth knowing about:
  **`axis_convention`** walks a known geodesic from the origin and asserts where
  it lands — no invariant can catch an E/N swap, because `x_off == y_off`;
  **`matches_pyproj`** proves the database's PROJ and pyproj's bundled PROJ
  agree (they are different builds); **`materialised_up_to_date`** compares the
  stored `geom_enu`/`location_enu` against a fresh transform, catching a frame
  edited without a `recompute_enu`.

**QGIS**: load `measurements` and pick `geom_enu` as the geometry column, then
set the **project CRS to 990001** so QGIS does no transform at all and E/N/U
are displayed verbatim.

**Never let anything reproject an ENU layer.** The `spatial_ref_sys` row for
990001 is a `+proj=ortho` definition — PROJ 9.6 cannot compose geographic →
topocentric as a CRS ("Mismatched units between step 2 and 3") and drops the
origin from topocentric WKT2, so ortho is the only definition QGIS/GDAL can
consume, but it is **not** interchangeable with the frame. Measured over the
survey area, `ST_Transform(geom_enu, 32632)` is off by:

- **horizontally, `h·d/N` — up to ~0.70 m.** Ortho projects geodetic
  (lat, lon) and ignores h, while topocentric E/N grow with distance from the
  geocentre; at 2 km altitude that term dominates. (The design note in
  `enu-postgis-plan.md` §6.2 claims <1 mm — that holds only for points *on*
  the ellipsoid and is wrong here. `check_ortho_display_crs` pins the real
  model.)
- **vertically, `d²/2R` — up to ~0.36 m**, because ortho passes ellipsoidal h
  through while the stored Z is height above the tangent plane.

`georef_from_enu()` is the only correct inverse.

### `surveys` — survey domain
- `Survey`, `Instrument`, `SurveyHasInstrument`, `Flight` — campaign metadata
  (legacy integer PKs, explicit `db_table`)
- `Point`, `Measurement`, `MeasurementPhoto` — GNSS points and yearly
  measurements. A DB trigger (`compute_measurement_geometry`, BEFORE
  INSERT/UPDATE on `measurements`) computes `geom`, `lat`, `lon`, `h_orto`
  from `east`/`north`/`h` using the `raster.geoid_model` geoid raster.
  A second, migration-managed trigger (`measurements_fill_geom_enu`) fills
  `geom_enu` (PointZ, SRID 990001) from `east`/`north`/`h`. It reads the source
  columns rather than `geom` — `geom` is 2D and `compute_measurement_geometry`
  is not under migration control, so depending on it would leave `geom_enu`
  NULL on fresh databases.
- `Product2D` (`products_2d`), `Product3D` (`products_3d`), `Volume`
  (`volumes`) — survey data products with S3 reference fields
  (`bucket`, `object_key`, etag/size, `is_uploaded`); bucket
  `S3_PRODUCTS_BUCKET_NAME` (default `belvedere-products`, not yet created);
  synced by `manage.py index_s3_products [--bucket B] [--dry-run]`.
- `MeasurementPhoto` (`measurement_photos`) — **several photos per
  measurement** (FK, `related_name="photos"`). Files live in the private
  bucket `S3_SURVEYS_BUCKET_NAME` (`belvedere-surveys`) under
  `S3_MEASUREMENT_PHOTOS_PREFIX` (`measurement_photos/`), key
  `<prefix><measurement_id>/<uuid><ext>` — a new key per upload, so a replaced
  photo is never served from cache. Storage is django-storages S3 built by
  `image_index.s3_utils.build_s3_storage()` (same client config as the other
  S3 helpers); `STORAGES["default"]` is local filesystem only so nothing else
  writes to S3. `file_name` is the original upload name. Django never deletes
  files by itself, so the model deletes the old object on replace and a
  `post_delete` receiver deletes it with the row (also on cascade from
  `Measurement`), both in `transaction.on_commit(robust=True)`.
  Deletes only touch keys under this environment's prefix, so a local copy
  (uploading under `dev/`) never deletes production's files in the shared bucket.
  **Field photos**: `manage.py import_point_photos DIR --year YYYY` attaches
  the controller's `Img_<LABEL>_<DDMMYY>_<HHMMSS>.jpg` photos to that year's
  measurements (`--alias` for misspelt labels), stored **upright** (EXIF
  orientation + `rotations.csv` in DIR, degrees counterclockwise, for untagged
  sideways photos — check the generated `_contact_sheet.jpg`), and sets
  `meas_time` (UTC) / `meas_date` from the earliest photo, names read as
  `--tz` (default Europe/Rome: the controller runs on local time). Dry-run by
  default; idempotent; `--replace-attached`, `--overwrite-times`.
  `meas_time` is `timestamptz` since `surveys/0033` (the live column was a
  legacy `timestamp without time zone`).
  **Monograph texts** (`surveys/0034`): `points.target`, `description`
  (+ `notes` for warnings and history), with a DB-level default `''` so QGIS
  inserts still work. The **survey mode is per measurement only**:
  `measurements.meas_strategy` (labelled "survey mode", varchar(100) since
  `surveys/0035`, which moved and dropped the short-lived `points.survey_mode`).
  `manage.py import_monograph_texts FILE.pub|.odg ...` (LibreOffice converts
  `.pub` -> `.odg`, so run it locally; parser in `surveys/utils/publisher.py`)
  fills target/description, merges NOTA into notes, and splits "Modalità di
  rilievo GNSS" (`split_survey_mode`): the current mode goes to the point's
  latest measurement **only if its survey mode is empty** (explicit values such
  as "RTK SPIN3" stay), the history ("RTK dal 2023, STATICO in precedenza")
  goes to the point's notes as "Survey mode history: ...". "E' un punto FISSO"
  is only checked against `is_fixed`. Dry-run by default; idempotent.
  **Points list** opens on **active points only** (`PointStatusFilter`,
  `?status=active|inactive|all`, with a warning naming how many inactive —
  history — points are hidden); the map and the monograph/CSV exports follow
  it. The raw-id picker popup (Measurement form) lists all points.
  **Monographs**: `manage.py generate_monographs [--until-year Y]
  [--points ...] [--include-inactive] [--split] [--output DIR]` writes one PDF
  (cover + index, one A4 page per point) from the database only
  (`surveys/monographs.py`, template `surveys/monograph.html`, WeasyPrint).
  Per point: latest measurement up to the year (`latest_measurement`), its
  survey mode, the point texts, coordinates in EPSG:7791 (E, N, H ITALGEO05),
  EPSG:4326 (DMS/decimal, h) and geocentric EPSG:6704 (pyproj; matches the
  2024 monographs to 0.4 mm), up to 2 photos (empty boxes otherwise). Overview
  map: Esri World Imagery tiles (`surveys/utils/basemap.py`), fetched once and
  cached in `MONOGRAPH_TILE_CACHE` (default `.cache/monograph_tiles`). All 52
  active points: ~30 s. **Admin**: Point edit form "View monograph" (HTML, stable
  photo URLs, the overview embedded once) / "Export monograph PDF"; a
  "Monograph: View · PDF" column in the Points list; list buttons export **all
  listed points** (current filters/search) as one HTML page or one PDF
  (`PointAdmin.get_urls`). PDFs need the Pango/HarfBuzz/DejaVu packages in the
  Docker image; `weasyprint` is imported only when a PDF is made.
  Stable URL `/surveys/photos/<id>/` (staff only) redirects to a 15-minute
  presigned link; the admin (inline on the Measurement form, standalone
  photo page) only ever links to it. Local development uses
  `S3_MEASUREMENT_PHOTOS_PREFIX=dev/measurement_photos/`.
  TODO: public access; downscaling / EXIF rotation / thumbnails; HEIC
  (rejected by Pillow today); QGIS (`photo_url` in `points_measurements`,
  relation on `measurements`); `photo_url` in `/surveys/measurements/`;
  orphaned-object cleanup command.
- Unmanaged read-only models over the Postgres views (`PointsMeasurement`,
  `PointMovement`, `ActivePoint`); view DDL lives in `surveys/sql.py`
  (`create_views()`), applied by migrations. Only `PointMovement` has an admin
  page; `points_measurements` and `active_points` are covered by the
  Measurement and Point pages and stay for QGIS.
- **`point_movements`** (since `surveys/0031`, replacing `points_movement_raw`
  and `points_movement_filtered`): one row per pair of consecutive
  measurements of a **moving** point (fixed marks excluded; no row for the
  first measurement of a series; identical consecutive coordinates skipped).
  Dates are `COALESCE(meas_date, survey date)`. `d`/`v` are **horizontal**,
  from `geom_enu` (true distances; UTM grid distances are ~0.06 % shorter
  here, and ENU `d_e`/`d_n` are true east/north, ~0.8 deg from grid axes);
  `d_3d`/`v_3d` include the vertical part, i.e. glacier surface lowering —
  typically 13 % above `v`, up to 3x for slow thinning stakes. Units m, m/yr,
  m/yr2 (365.25-day year). `is_annual` = 280 < dt < 400 days; intra-annual and
  multi-year pairs are kept for future use. Acceleration
  `a = (v_i - v_(i-1)) / ((dt_i + dt_(i-1))/2)` — the mean velocity of an
  interval is the velocity at its midpoint, so this is exact under constant
  acceleration and 0 at constant speed — only between two contiguous annual
  intervals. (The old views used `d/dt^2`, which is not an acceleration.)
  `geom_line` (7791) is the displacement line for QGIS; the admin map draws
  the same lines from `lat/lon_prev` -> `lat/lon_fin`.

Public landing page at `/` (`api_home`, template `surveys/api_home.html`; also the admin's "View site"): endpoints with live examples, field meanings, licence/citation, links. Keep it in step with the URLs below.

API under `/surveys/` (plain JSON arrays, no pagination):
- `years/` — distinct survey years with measurements
- `measurements/?year=YYYY&is_fixed=true|false` — field names match the
  legacy `points_measurements` view
- `points/<label>/velocity/` — yearly **horizontal** velocity series in
  **m/day**, read from `point_movements` (`is_annual` rows, `v / 365.25`);
  same response shape as before. Before `surveys/0031` `v` was 3D.

### `image_index` — time-lapse camera catalogue
- `Camera`: S3 bucket/prefix, 3D PostGIS `location` (32632) plus `location_enu`
  (990001). Both the coordinate-column sync and the ENU fill live in the single
  `image_index_camera_sync_location()` trigger function — Postgres fires
  BEFORE-row triggers in name order, so a second trigger could read a stale
  `location`. `is_active` controls indexing.
- `CameraCalibration`: intrinsics/extrinsics as `ArrayField`; `CameraModel`
  IntEnum (includes custom `METASHAPE=11`)
- `Image`: one row per S3 object; `(bucket, object_key)` unique;
  `datetime`/`rotation` derived at save time from shared helpers

API under `/cams/`: `cameras/`, `images/` (DRF, paginated),
`images/<pk>/preview/`, `thumb/`, `preview-url/`, `thumb-url/`
(S3 proxy / presigned URLs). Reverse with
`reverse("image_index:serve_image_preview", args=[pk])`.

## Shared modules (single source of truth — do not duplicate)

- `image_index/s3_utils.py`: `build_s3_client()`, `build_readonly_s3_client()`,
  `get_object_bytes()`, `put_object_bytes()`, `generate_presigned_url()` —
  used by both apps
- `image_index/image_metadata.py`: `IMAGE_EXTENSIONS`, `FILENAME_DATETIME_PATTERNS`
  (ordered list, first match wins), `parse_datetime_from_filename()`,
  `parse_datetime_from_exif_dict()`, `make_json_safe()`,
  `extract_image_metadata_from_bytes()`

## Infrastructure

- Settings via `python-decouple` (`.env`); static files via `whitenoise`;
  CORS open; DRF anon throttle 300/min
- S3 (Hetzner object storage): `s3v4` signature + virtual addressing;
  `S3_ENDPOINT_URL` required for the indexing commands; buckets:
  `belvedere-images` (camera images), `belvedere-products` (survey products)
- External DB consumers: QGIS (`layer_styles`, `qgis_projects`, the views);
  viewers consume only the HTTP API
