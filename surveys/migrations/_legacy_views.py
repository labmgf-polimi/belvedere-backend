"""View DDL as it stood before the `ds_*` -> `std_*` rename in `surveys/0024`.

Frozen on purpose. `surveys/sql.py` describes the *current* schema, so
migrations that run before the rename — `0022` on a fresh database, and the
reverse of `0024` — cannot use it: they would emit `meas.std_east` against a
table whose column is still `ds_east`.

Not named `0*.py`, so Django does not mistake it for a migration.
"""

POINTS_MEASUREMENTS_SQL = """
CREATE OR REPLACE VIEW public.points_measurements AS
 SELECT meas.id,
    meas.geom,
    pts.label,
    meas.point AS point_id,
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
    meas.ds_east,
    meas.ds_north,
    meas.ds_h
   FROM measurements meas
     JOIN surveys sur ON meas.survey = sur.id
     JOIN points pts ON meas.point = pts.id
  ORDER BY sur.year DESC, pts.label;
"""


# The two movement views as they stood until `surveys/0031` replaced them with
# `point_movements`.
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


def create_views() -> str:
    """The four views with the pre-rename `points_measurements`.

    `active_points` never referenced the renamed columns, so it still comes
    from `surveys.sql`.
    """
    from surveys.sql import ACTIVE_POINTS_SQL

    return (
        POINTS_MEASUREMENTS_SQL
        + POINTS_MOVEMENT_RAW_SQL
        + POINTS_MOVEMENT_FILTERED_SQL
        + ACTIVE_POINTS_SQL
    )
