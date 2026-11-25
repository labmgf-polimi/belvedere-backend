"""DDL for the Postgres views and triggers owned by the `surveys` app.

Kept here rather than inside a migration because more than one migration needs
it: any change to `measurements.geom` forces the dependent views to be dropped
and recreated, so the definitions must come from one place.

The view bodies are the ones adopted in `surveys/migrations/0011_adopt_views.py`,
which keeps its own copy as the record of what was originally applied.

**Any change to a view must go through a new migration** — never manual DDL.
"""

from georef.constants import ENU_SRID, PROJECT_SRID

#: Field descriptions, keyed by column name — unit, CRS and meaning, nothing
#: more. QGIS renders them in a narrow column, so they stay one line.
#:
#: Single source for both the model fields (`db_comment=`) and the view
#: comments below, so a column cannot be described one way on the table and
#: another way on the view QGIS actually opens.
COLUMN_COMMENTS: dict[str, str] = {
    # --- measurements ---
    "east": "Easting, m, EPSG:7791 (RDN2008 / UTM 32N).",
    "north": "Northing, m, EPSG:7791 (RDN2008 / UTM 32N).",
    "h": "Ellipsoidal height, m (GRS80).",
    "h_orto": "Orthometric height, m. Derived from h via the ITALGEO05 geoid.",
    "lat": "Latitude, decimal degrees, WGS 84. Derived from east/north.",
    "lon": "Longitude, decimal degrees, WGS 84. Derived from east/north.",
    "std_east": "Standard deviation of the easting, m.",
    "std_north": "Standard deviation of the northing, m.",
    "std_h": "Standard deviation of the height, m.",
    "geom": "Measurement position, EPSG:7791. Derived from east/north by trigger.",
    "geom_enu": (
        "Local ENU position, m (SRID 990001). Z is height above the tangent "
        "plane at D12 plus a 1000 m offset - NOT an altitude."
    ),
    "meas_date": "Date the point was measured.",
    "meas_time": "Timestamp of the measurement, stored in UTC (field controller time converted).",
    "meas_strategy": "Survey mode of this measurement (GNSS method), e.g. 'RTK RADIO' (own base station).",
    "datum_realization": "Reference frame realization the campaign was processed in.",
    "height_type": "Whether h is ellipsoidal or orthometric. Ellipsoidal everywhere.",
    "notes": "Free-text notes.",
    # --- measurement_photos ---
    "measurement_id": "Measurement the photo belongs to.",
    "image": "S3 object key of the photo in the surveys bucket (private).",
    "file_name": "Original name of the uploaded file.",
    "uploaded_at": "When the current file was uploaded.",
    # --- points ---
    "label": "Point label, e.g. 'D12'.",
    "is_fixed": "True for stable reference marks; false for stakes on moving ice.",
    "active": "Whether the point is still part of the monitoring network.",
    # `points.active` is exposed as `is_active` by the views, to read alongside
    # `is_fixed`.
    "is_active": "Whether the point is still part of the monitoring network.",
    "ref_date": "Date the point was established.",
    "target": "Target on the point: colours and fixing, e.g. 'giallo e nero (2 viti)'.",
    "description": "Where the point is and how to find it.",
    # --- surveys ---
    "date": "Campaign date.",
    "year": "Campaign year.",
    "survey_date": "Campaign date.",
    "survey_year": "Campaign year.",
    "last_measure_date": "Date of the most recent measurement of this point.",
    # --- point_movements view ---
    "point_id": "Id of the monitoring point.",
    "geom_line": "Displacement line from the earlier to the later position, EPSG:7791.",
    "date_prev": "Start of the movement: date of the earlier measurement (measurement date, else campaign date).",
    "date_fin": "End of the movement: date of the later measurement (measurement date, else campaign date).",
    "dt": "Days between the two measurements.",
    "is_annual": "True when the two measurements are about a year apart (280 < dt < 400 days).",
    "d_e": "Displacement east, m (true east, local ENU frame).",
    "d_n": "Displacement north, m (true north, local ENU frame).",
    "d": "Horizontal displacement, m. The main displacement value.",
    "d_h": "Vertical displacement (change of ellipsoidal height), m.",
    "d_3d": "3D displacement, m. Includes the vertical part, i.e. glacier surface lowering.",
    "v_e": "Velocity east, m/yr.",
    "v_n": "Velocity north, m/yr.",
    "v": "Horizontal velocity, m/yr. The main velocity value.",
    "v_h": "Vertical velocity, m/yr (negative = surface lowering).",
    "v_3d": "3D velocity, m/yr. Includes glacier surface lowering.",
    "a_e": "Acceleration east, m/yr2. Annual consecutive intervals only.",
    "a_n": "Acceleration north, m/yr2. Annual consecutive intervals only.",
    "a": "Change of horizontal velocity, m/yr2 (positive = speeding up). Annual consecutive intervals only.",
    "lat_prev": "Latitude of the earlier position, WGS 84.",
    "lon_prev": "Longitude of the earlier position, WGS 84.",
    "lat_fin": "Latitude of the later position, WGS 84.",
    "lon_fin": "Longitude of the later position, WGS 84.",
}

TABLE_COMMENTS: dict[str, str] = {
    "measurements": "One GNSS measurement of a point during a survey campaign.",
    "points": "Monitoring points: stakes on the glacier and fixed reference marks.",
    "surveys": "Survey campaigns.",
    "measurement_photos": "Photos of a measurement; files live in the private S3 surveys bucket.",
}


def comment_columns(
    relation: str,
    columns: tuple[str, ...],
    *,
    drop: bool = False,
    overrides: dict[str, str] | None = None,
) -> str:
    """`COMMENT ON COLUMN` for the columns of a table or view.

    Views do not inherit column comments from their base tables, and dropping
    a view drops its comments — so this is appended to the view DDL and
    reapplied on every rebuild. `overrides` replaces the shared wording where a
    column means something narrower on this relation.
    """
    texts = COLUMN_COMMENTS | (overrides or {})
    statements = [
        "COMMENT ON COLUMN public.{}.{} IS {};".format(
            relation, column, "NULL" if drop else _quote(texts[column])
        )
        for column in columns
        if column in texts
    ]
    return "\n".join(statements) + "\n"


def comment_table(table: str, *, drop: bool = False) -> str:
    value = "NULL" if drop else _quote(TABLE_COMMENTS[table])
    return f"COMMENT ON TABLE public.{table} IS {value};\n"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def points_measurements_sql() -> str:
    return """
CREATE OR REPLACE VIEW public.points_measurements AS
 SELECT meas.id,
    meas.geom,
    pts.label,
    meas.point AS point_id,
    pts.active AS is_active,
    pts.is_fixed,
    meas.east,
    meas.north,
    meas.h_orto,
    meas.lat,
    meas.lon,
    meas.h,
    meas.survey AS survey_id,
    sur.date AS survey_date,
    sur.year AS survey_year,
    meas.meas_date,
    meas.meas_time,
    meas.meas_strategy,
    meas.std_east,
    meas.std_north,
    meas.std_h
   FROM measurements meas
     JOIN surveys sur ON meas.survey = sur.id
     JOIN points pts ON meas.point = pts.id
  ORDER BY sur.year DESC, pts.label;
"""


#: Days per year for the m/yr units (Julian year).
DAYS_PER_YEAR = 365.25

#: Gap, in days, between two measurements that counts as one year apart.
ANNUAL_DT_MIN, ANNUAL_DT_MAX = 280, 400

POINT_MOVEMENTS_SQL = f"""
CREATE OR REPLACE VIEW public.point_movements AS
 WITH series AS (
         -- Moving points only: fixed reference marks never get a movement.
         SELECT m.id,
            m.point AS point_id,
            p.label,
            s.year AS survey_year,
            COALESCE(m.meas_date, s.date) AS day,
            m.geom,
            m.lat,
            m.lon,
            m.h,
            -- Horizontal displacements in the local ENU frame: true metric
            -- distances, unlike UTM grid distances (~0.06 % longer here).
            ST_X(m.geom_enu) AS e,
            ST_Y(m.geom_enu) AS n
           FROM measurements m
             JOIN points p ON p.id = m.point
             JOIN surveys s ON s.id = m.survey
          WHERE NOT p.is_fixed
        ), pairs AS (
         -- Each measurement paired with the previous one of the same point.
         SELECT id,
            point_id,
            label,
            survey_year,
            lag(day) OVER w AS date_prev,
            day AS date_fin,
            day - lag(day) OVER w AS dt,
            ST_MakeLine(lag(geom) OVER w, geom) AS geom_line,
            lag(lat) OVER w AS lat_prev,
            lag(lon) OVER w AS lon_prev,
            lat AS lat_fin,
            lon AS lon_fin,
            e - lag(e) OVER w AS d_e,
            n - lag(n) OVER w AS d_n,
            h - lag(h) OVER w AS d_h
           FROM series
          WINDOW w AS (PARTITION BY point_id ORDER BY day, id)
        ), rates AS (
         -- The first measurement of a series has no pair; identical
         -- consecutive coordinates are a copied value, not a movement.
         SELECT pairs.*,
            sqrt(d_e ^ 2 + d_n ^ 2) AS d,
            sqrt(d_e ^ 2 + d_n ^ 2 + d_h ^ 2) AS d_3d,
            dt > {ANNUAL_DT_MIN} AND dt < {ANNUAL_DT_MAX} AS is_annual,
            -- Computed in m/day, exposed in m/yr.
            d_e / dt * {DAYS_PER_YEAR} AS v_e,
            d_n / dt * {DAYS_PER_YEAR} AS v_n,
            sqrt(d_e ^ 2 + d_n ^ 2) / dt * {DAYS_PER_YEAR} AS v,
            d_h / dt * {DAYS_PER_YEAR} AS v_h,
            sqrt(d_e ^ 2 + d_n ^ 2 + d_h ^ 2) / dt * {DAYS_PER_YEAR} AS v_3d
           FROM pairs
          WHERE dt > 0 AND (d_e <> 0 OR d_n <> 0 OR d_h <> 0)
        ), previous AS (
         SELECT rates.*,
            lag(date_fin) OVER w AS prev_date_fin,
            lag(is_annual) OVER w AS prev_is_annual,
            lag(dt) OVER w AS prev_dt,
            lag(v_e) OVER w AS prev_v_e,
            lag(v_n) OVER w AS prev_v_n,
            lag(v) OVER w AS prev_v
           FROM rates
          WINDOW w AS (PARTITION BY point_id ORDER BY date_fin, id)
        )
 -- Acceleration: a mean velocity over an interval is the velocity at its
 -- midpoint, so a = (v_i - v_(i-1)) / (time between the two midpoints)
 --             = (v_i - v_(i-1)) / ((dt_i + dt_(i-1)) / 2),
 -- exact under constant acceleration and 0 at constant speed. Only between
 -- two annual intervals that share a measurement.
 SELECT id,
    point_id,
    label,
    survey_year,
    date_prev,
    date_fin,
    dt,
    is_annual,
    geom_line,
    d_e,
    d_n,
    d,
    d_h,
    d_3d,
    v_e,
    v_n,
    v,
    v_h,
    v_3d,
    CASE WHEN is_annual AND prev_is_annual AND prev_date_fin = date_prev
         THEN (v_e - prev_v_e) / ((dt + prev_dt) / 2.0 / {DAYS_PER_YEAR}) END AS a_e,
    CASE WHEN is_annual AND prev_is_annual AND prev_date_fin = date_prev
         THEN (v_n - prev_v_n) / ((dt + prev_dt) / 2.0 / {DAYS_PER_YEAR}) END AS a_n,
    CASE WHEN is_annual AND prev_is_annual AND prev_date_fin = date_prev
         THEN (v - prev_v) / ((dt + prev_dt) / 2.0 / {DAYS_PER_YEAR}) END AS a,
    lat_prev,
    lon_prev,
    lat_fin,
    lon_fin
   FROM previous
  ORDER BY label, date_fin DESC;
"""

ACTIVE_POINTS_SQL = """
CREATE OR REPLACE VIEW public.active_points AS
 WITH lastmeasurement AS (
         SELECT meas.id AS meas_id,
            meas.geom,
            points.label,
            surveys.date AS last_measure_date,
            meas.east,
            meas.north,
            meas.h,
            points.is_fixed,
            row_number() OVER (PARTITION BY points.label ORDER BY surveys.date DESC) AS row_num
           FROM measurements meas
             JOIN points ON meas.point = points.id
             JOIN surveys ON meas.survey = surveys.id
          WHERE points.active = true
        )
 SELECT meas_id,
    geom,
    label,
    last_measure_date,
    east,
    north,
    h,
    is_fixed
   FROM lastmeasurement
  WHERE row_num = 1;
"""


#: Columns each view exposes, for the comments below.
POINTS_MEASUREMENTS_COLUMNS = (
    "geom", "label", "is_active", "is_fixed", "east", "north", "h_orto", "lat",
    "lon", "h", "survey_date", "survey_year", "meas_date", "meas_time",
    "meas_strategy", "std_east", "std_north", "std_h",
)  # fmt: skip


#: Where a shared column name means something narrower on point_movements.
POINT_MOVEMENTS_COMMENTS = {
    "survey_year": "Campaign year of the later (end) measurement: the movement covers date_prev -> date_fin.",
}

POINT_MOVEMENTS_COLUMNS = (
    "point_id", "label", "survey_year", "date_prev", "date_fin", "dt",
    "is_annual", "geom_line", "d_e", "d_n", "d", "d_h", "d_3d", "v_e", "v_n",
    "v", "v_h", "v_3d", "a_e", "a_n", "a", "lat_prev", "lon_prev", "lat_fin",
    "lon_fin",
)  # fmt: skip

ACTIVE_POINTS_COLUMNS = (
    "geom", "label", "last_measure_date", "east", "north", "h", "is_fixed",
)  # fmt: skip


def create_views() -> str:
    """View DDL plus column comments, in dependency order.

    The comments follow each definition rather than living in a separate
    migration, because dropping a view drops its comments — this way a rebuild
    never leaves QGIS without field metadata.

    This describes the *current* schema. Migrations that predate a column
    rename carry their own snapshot in `surveys/migrations/_legacy_views.py`.
    """
    return (
        points_measurements_sql()
        + comment_columns("points_measurements", POINTS_MEASUREMENTS_COLUMNS)
        + POINT_MOVEMENTS_SQL
        + comment_columns(
            "point_movements",
            POINT_MOVEMENTS_COLUMNS,
            overrides=POINT_MOVEMENTS_COMMENTS,
        )
        + ACTIVE_POINTS_SQL
        + comment_columns("active_points", ACTIVE_POINTS_COLUMNS)
    )


CREATE_VIEWS = create_views()

#: Reverse order, so nothing is dropped while still depended on. The two
#: `points_movement_*` views were replaced by `point_movements` in surveys/0031;
#: they stay listed because older migrations (0024, 0028) run this on fresh
#: databases where they still exist.
DROP_VIEWS = """
DROP VIEW IF EXISTS public.point_movements;
DROP VIEW IF EXISTS public.points_movement_filtered;
DROP VIEW IF EXISTS public.points_movement_raw;
DROP VIEW IF EXISTS public.active_points;
DROP VIEW IF EXISTS public.points_measurements;
"""


def compute_measurement_geometry(srid: int = PROJECT_SRID) -> str:
    """Derive `geom`, `lat`, `lon` and `h_orto` from east/north/h.

    Under migration control since `surveys/0022`; before that it existed only
    on the live database. Two things it now does that the original did not:

    * samples the geoid **in the raster's own SRID**, because the raster is
      tagged 32632 while the measurements are 7791 — mixing them would make
      every insert fail, and re-tagging the raster is out of scope (it is
      maintained outside Django);
    * skips the geoid entirely when `raster.geoid_model` is absent, which is
      the case on test databases. `h_orto` then stays NULL, exactly as it did
      when the trigger did not exist there at all.
    """
    return f"""
CREATE OR REPLACE FUNCTION compute_measurement_geometry()
RETURNS TRIGGER AS $$
BEGIN
    NEW.geom := ST_SetSRID(ST_MakePoint(NEW.east, NEW.north), {srid});
    NEW.lat := ST_Y(ST_Transform(NEW.geom, 4326));
    NEW.lon := ST_X(ST_Transform(NEW.geom, 4326));

    IF to_regclass('raster.geoid_model') IS NOT NULL THEN
        SELECT NEW.h - ST_Value(
                   rast, 1, ST_Transform(NEW.geom, ST_SRID(rast)),
                   exclude_nodata_value => true, resample => 'bilinear')
          INTO NEW.h_orto
          FROM raster.geoid_model rast
         WHERE ST_Intersects(rast, ST_Transform(NEW.geom, ST_SRID(rast)))
         LIMIT 1;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER compute_measurement_geometry_trigger
BEFORE INSERT OR UPDATE ON measurements
FOR EACH ROW EXECUTE FUNCTION compute_measurement_geometry();
"""


def measurements_fill_geom_enu(
    srid: int = PROJECT_SRID, enu_srid: int = ENU_SRID
) -> str:
    """Fill `geom_enu` from east/north/h. See `surveys/0020` for the rationale."""
    return f"""
CREATE OR REPLACE FUNCTION measurements_fill_geom_enu()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.east IS NULL OR NEW.north IS NULL OR NEW.h IS NULL THEN
        NEW.geom_enu := NULL;
    ELSE
        NEW.geom_enu := georef_to_enu(
            ST_SetSRID(ST_MakePoint(NEW.east, NEW.north, NEW.h), {srid}),
            {enu_srid}
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
