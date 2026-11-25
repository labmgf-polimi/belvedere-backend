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
    "meas_time": "Timestamp of the measurement.",
    "meas_strategy": "GNSS method and receiver, e.g. 'RTK SPIN3'.",
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
    # --- surveys ---
    "date": "Campaign date.",
    "year": "Campaign year.",
    "survey_date": "Campaign date.",
    "survey_year": "Campaign year.",
    "last_measure_date": "Date of the most recent measurement of this point.",
    # --- movement views ---
    "dt": "Days between the two surveys.",
    "d_e": "Displacement east between consecutive surveys, m.",
    "d_n": "Displacement north between consecutive surveys, m.",
    "d_h": "Displacement in height between consecutive surveys, m.",
    "d": "3D displacement between consecutive surveys, m.",
    "v_e": "Velocity east, m/day.",
    "v_n": "Velocity north, m/day.",
    "v_h": "Vertical velocity, m/day.",
    "v": "3D velocity magnitude, m/day.",
    "a_e": "Acceleration east, m/day2.",
    "a_n": "Acceleration north, m/day2.",
    "a_h": "Vertical acceleration, m/day2.",
    "a": "3D acceleration magnitude, m/day2.",
    "survey_date_fin": "Date of the later survey of the pair.",
    "survey_date_prev": "Date of the earlier survey of the pair.",
    "east_fin": "Easting at the later survey, m, EPSG:7791.",
    "north_fin": "Northing at the later survey, m, EPSG:7791.",
    "h_fin": "Ellipsoidal height at the later survey, m.",
    "east_prev": "Easting at the earlier survey, m, EPSG:7791.",
    "north_prev": "Northing at the earlier survey, m, EPSG:7791.",
    "h_prev": "Ellipsoidal height at the earlier survey, m.",
}

TABLE_COMMENTS: dict[str, str] = {
    "measurements": "One GNSS measurement of a point during a survey campaign.",
    "points": "Monitoring points: stakes on the glacier and fixed reference marks.",
    "surveys": "Survey campaigns.",
    "measurement_photos": "Photos of a measurement; files live in the private S3 surveys bucket.",
}


def comment_columns(
    relation: str, columns: tuple[str, ...], *, drop: bool = False
) -> str:
    """`COMMENT ON COLUMN` for the columns of a table or view.

    Views do not inherit column comments from their base tables, and dropping
    a view drops its comments — so this is appended to the view DDL and
    reapplied on every rebuild.
    """
    statements = [
        "COMMENT ON COLUMN public.{}.{} IS {};".format(
            relation, column, "NULL" if drop else _quote(COLUMN_COMMENTS[column])
        )
        for column in columns
        if column in COLUMN_COMMENTS
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


POINTS_MOVEMENT_RAW_SQL = """
CREATE OR REPLACE VIEW public.points_movement_raw AS
 WITH laggedmeasurements AS (
         SELECT meas_1.id,
            lag(meas_1.east) OVER (PARTITION BY meas_1.label ORDER BY meas_1.survey_date) AS prev_east,
            lag(meas_1.north) OVER (PARTITION BY meas_1.label ORDER BY meas_1.survey_date) AS prev_north,
            lag(meas_1.h) OVER (PARTITION BY meas_1.label ORDER BY meas_1.survey_date) AS prev_h,
            lag(meas_1.survey_date) OVER (PARTITION BY meas_1.label ORDER BY meas_1.survey_date) AS prev_survey_date
           FROM points_measurements meas_1
        ), diffs AS (
         SELECT meas_1.id,
            COALESCE(meas_1.survey_date - lm_1.prev_survey_date, 0) AS dt,
            COALESCE(meas_1.east - lm_1.prev_east, 0::double precision) AS d_e,
            COALESCE(meas_1.north - lm_1.prev_north, 0::double precision) AS d_n,
            COALESCE(meas_1.h - lm_1.prev_h, 0::double precision) AS d_h
           FROM points_measurements meas_1
             JOIN laggedmeasurements lm_1 ON meas_1.id = lm_1.id
        )
 SELECT meas.id,
    meas.geom,
    meas.label,
    meas.survey_year,
    meas.is_fixed,
    meas.survey_date AS survey_date_fin,
    lm.prev_survey_date AS survey_date_prev,
    diffs.dt,
    meas.east AS east_fin,
    meas.north AS north_fin,
    meas.h AS h_fin,
    lm.prev_east AS east_prev,
    lm.prev_north AS north_prev,
    lm.prev_h AS h_prev,
    diffs.d_e,
    diffs.d_n,
    diffs.d_h,
    COALESCE(sqrt(pow(diffs.d_e, 2::double precision) + pow(diffs.d_n, 2::double precision) + pow(diffs.d_h, 2::double precision)), 0::double precision) AS d,
    COALESCE(diffs.d_e / NULLIF(diffs.dt, 0)::double precision, 0::double precision) AS v_e,
    COALESCE(diffs.d_n / NULLIF(diffs.dt, 0)::double precision, 0::double precision) AS v_n,
    COALESCE(diffs.d_h / NULLIF(diffs.dt, 0)::double precision, 0::double precision) AS v_h,
    COALESCE(sqrt(pow(diffs.d_e, 2::double precision) + pow(diffs.d_n, 2::double precision) + pow(diffs.d_h, 2::double precision)) / NULLIF(diffs.dt, 0)::double precision, 0::double precision) AS v,
    COALESCE(diffs.d_e / NULLIF(pow(diffs.dt::double precision, 2::double precision), 0::double precision), 0::double precision) AS a_e,
    COALESCE(diffs.d_n / NULLIF(pow(diffs.dt::double precision, 2::double precision), 0::double precision), 0::double precision) AS a_n,
    COALESCE(diffs.d_h / NULLIF(pow(diffs.dt::double precision, 2::double precision), 0::double precision), 0::double precision) AS a_h,
    COALESCE(sqrt(pow(diffs.d_e, 2::double precision) + pow(diffs.d_n, 2::double precision) + pow(diffs.d_h, 2::double precision)) / NULLIF(pow(diffs.dt::double precision, 2::double precision), 0::double precision), 0::double precision) AS a
   FROM points_measurements meas
     JOIN laggedmeasurements lm ON meas.id = lm.id
     JOIN diffs ON meas.id = diffs.id;
"""

POINTS_MOVEMENT_FILTERED_SQL = """
CREATE OR REPLACE VIEW public.points_movement_filtered AS
 SELECT id,
    geom,
    label,
    survey_year,
    is_fixed,
    survey_date_fin,
    survey_date_prev,
    dt,
    east_fin,
    north_fin,
    h_fin,
    east_prev,
    north_prev,
    h_prev,
    d_e,
    d_n,
    d_h,
    d,
    v_e,
    v_n,
    v_h,
    v,
    a_e,
    a_n,
    a_h,
    a
   FROM points_movement_raw
  WHERE is_fixed = false AND dt > 280 AND dt < 400 AND d > 0::double precision
  ORDER BY label, survey_date_fin DESC;
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


POINTS_MOVEMENT_COLUMNS = (
    "geom", "label", "survey_year", "is_fixed", "survey_date_fin",
    "survey_date_prev", "dt", "east_fin", "north_fin", "h_fin", "east_prev",
    "north_prev", "h_prev", "d_e", "d_n", "d_h", "d", "v_e", "v_n", "v_h", "v",
    "a_e", "a_n", "a_h", "a",
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
        + POINTS_MOVEMENT_RAW_SQL
        + comment_columns("points_movement_raw", POINTS_MOVEMENT_COLUMNS)
        + POINTS_MOVEMENT_FILTERED_SQL
        + comment_columns("points_movement_filtered", POINTS_MOVEMENT_COLUMNS)
        + ACTIVE_POINTS_SQL
        + comment_columns("active_points", ACTIVE_POINTS_COLUMNS)
    )


CREATE_VIEWS = create_views()

#: Reverse order, so nothing is dropped while still depended on.
DROP_VIEWS = """
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
