"""`measurements.meas_time` becomes a real `timestamptz`, documented as UTC.

Django's DateTimeField is `timestamp with time zone`, which is what a fresh
database gets, but the live column predates Django and is `timestamp without
time zone`. The 5 values it held were written by Django, i.e. UTC wall time,
so they are read as UTC. Converting needs the views dropped (Postgres will not
alter a column a view uses) and rebuilt — which also re-applies their column
comments, including this one. On a fresh database the type is already right
and only the comment changes.

The times themselves are set from the field photos by `import_point_photos`.
"""

from django.db import migrations, models

from surveys.sql import COLUMN_COMMENTS, CREATE_VIEWS, DROP_VIEWS

TO_TIMESTAMPTZ = """
DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'measurements'
           AND column_name = 'meas_time') = 'timestamp without time zone' THEN
        ALTER TABLE public.measurements
            ALTER COLUMN meas_time TYPE timestamptz USING meas_time AT TIME ZONE 'UTC';
    END IF;
END $$;
"""

COMMENT = "COMMENT ON COLUMN public.measurements.meas_time IS '{}';\n".format(
    COLUMN_COMMENTS["meas_time"].replace("'", "''")
)


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0032_point_movements_dates"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="measurement",
                    name="meas_time",
                    field=models.DateTimeField(
                        blank=True, db_comment=COLUMN_COMMENTS["meas_time"], null=True
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=DROP_VIEWS + TO_TIMESTAMPTZ + COMMENT + CREATE_VIEWS,
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
        ),
    ]
