# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Response style

Keep all answers short and accurate. No verbose explanations, no padding. Go straight to the point. When showing code, show only the relevant diff or snippet. Do not include the full file unless explicitly asked. Always use markdown formatting for code.

## Session start

On a fresh session, read the `context/` files before editing anything:
- `context/architecture.md` — apps, models, API endpoints, shared modules, infrastructure
- `context/django-migration-status.md` — DB state, migration history, what stays outside Django

Inspect current source files before proposing architecture changes.

## Commands

```bash
uv sync                                          # install deps
uv run python manage.py runserver                # dev server
uv run python manage.py migrate
uv run python manage.py makemigrations
scripts/localdb.sh test                          # all tests, on the local PostGIS (compose.dev.yml)
scripts/localdb.sh test surveys.tests.TestClass.test_method  # single test
scripts/localdb.sh pull-live                     # overwrite the LOCAL copy with live data (live only read)
uv run python manage.py import_point_photos data/point_photos --year 2026 [--alias A=B] [--apply]  # field photos + measurement times
uv run python manage.py import_monograph_texts data/Monografie_*.pub [--apply]  # point texts from Publisher (needs LibreOffice)
scripts/localdb.sh run | manage <cmd>            # work against the local copy (.env.local)
uv run ruff check . && uv run ruff format .      # lint + format
uv run python manage.py index_s3_images [--camera-id ID] [--dry-run] [--force] [--workers N]
uv run python manage.py validate_reference_frame [--srid 990001]   # frame checks (read-only)
uv run python manage.py freeze_reference_frame  [--srid 990001]    # validate, then lock
uv run python manage.py recompute_enu [--target measurements|cameras|all] [--dry-run]
uv run python manage.py dump_reference_frame [--output PATH]       # export params to VCS
uv run python manage.py collectstatic --noinput  # production
```

## Hard rules

- **Database is fully Django-managed** (single `public` schema). Every schema change — tables **and Postgres views** — goes through a migration. Never apply manual DDL (pgAdmin/QGIS) to project relations.
- Run tests with `scripts/localdb.sh test`; plain `manage.py test` refuses a non-local `DB_HOST` so test databases are never created on the shared server.
- The live DB is shared with QGIS users; guard data-touching migrations and take a `pg_dump` before destructive DDL.
- Three apps (`surveys`, `image_index`, `georef`), one PostgreSQL/PostGIS database. Never split into separate databases.
- **The local ENU frame (SRID 990001) is immutable once `frozen`.** Never `UPDATE` the origin or offsets of a frozen `georef_reference_frame` row — every coordinate ever stored in that frame would silently change meaning. A correction means a **new SRID**. Enforced by a DB trigger.
- **ENU Z is not an altitude.** It is height above the tangent plane at D12 plus a neutral `z_off = 1000`, deliberately chosen so it cannot be misread as a height. Exact heights are `measurements.h` (ellipsoidal) and `h_orto` (orthometric).
- **Never reproject an ENU layer.** `spatial_ref_sys` defines 990001 as `+proj=ortho` (the only thing QGIS/GDAL can consume), which is *not* interchangeable with the frame: `ST_Transform(geom_enu, ...)` is off by ~0.70 m horizontally (`h·d/N`) and ~0.36 m vertically (`d²/2R`). Keep the QGIS project CRS at 990001, and use `georef_from_enu()` as the inverse.
- Reuse the shared S3/metadata helpers (`image_index/s3_utils.py`, `image_index/image_metadata.py`) — single source of truth, do not duplicate logic.
- **The project CRS is EPSG:7791** (RDN2008 / UTM 32N — ETRF2000, epoch 2008.0), *not* EPSG:32632. 32632 names the WGS 84 datum *ensemble* (2 m accuracy); RDN2008 is a single realization, which is what the Italian network delivers. The numbers are identical (PROJ treats RDN2008 → WGS 84 as a null transform) but the label matters: WGS 84 ≈ ITRF has drifted ~45 cm from ETRF2000 since 2008.0. Use `georef.constants.PROJECT_SRID`, never a literal.
- **Never hand-roll geodesy.** PostGIS/PROJ does it in the database; `pyproj` does it in Python (`georef/enu.py`, `georef/crs.py`). Note the two carry separate PROJ builds — `check_matches_pyproj` proves they agree.
- External viewers consume the HTTP API (`/surveys/`, `/cams/`); keep API response shapes backward-compatible.

## Coding standards

- **Always** use type annotations.
- **Docstrings:** Google style, only when non-trivial. One-line max for simple functions.
- **Comments:** only for non-obvious logic.
- Single-purpose functions. No over-engineering. No premature abstractions.
- By default assume tests should be written for new code; ask if unclear.
