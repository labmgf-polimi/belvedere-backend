"""One survey mode, on the measurement: `points.survey_mode` -> `measurements.meas_strategy`.

`survey_mode` (0034) held the Publisher monographs' "Modalità di rilievo
GNSS", which describes a point across campaigns. The survey mode belongs to
each measurement, so:

1. `meas_strategy` (labelled "survey mode") grows to 100 characters — the
   views are dropped and rebuilt around the type change, as in 0033;
2. each point's text is split (`surveys.utils.publisher.split_survey_mode`):
   the current mode goes to its latest measurement **only if that has no
   survey mode yet** (explicit values such as "RTK SPIN3" stay); the history
   ("RTK dal 2023, STATICO in precedenza") is appended to the point's notes.
   A plain "RTK" on a 2026 measurement becomes "RTK RADIO": that campaign
   used base-rover RTK with the project's own base station;
3. `points.survey_mode` is dropped.

Reverse restores the column empty (the moved values stay where they went).
"""

from django.db import migrations, models
from django.db.models.functions import Coalesce

from surveys.sql import COLUMN_COMMENTS, CREATE_VIEWS, DROP_VIEWS
from surveys.utils.publisher import history_note, split_survey_mode

PLACEHOLDER_NOTES = {"Automatically created during measurement CSV import"}
NOTES_MAX = 512

WIDEN = (
    DROP_VIEWS
    + "ALTER TABLE public.measurements ALTER COLUMN meas_strategy TYPE varchar(100);\n"
    + "COMMENT ON COLUMN public.measurements.meas_strategy IS '{}';\n".format(
        COLUMN_COMMENTS["meas_strategy"].replace("'", "''")
    )
    + CREATE_VIEWS
)


def move_survey_mode(apps, schema_editor) -> None:
    Point = apps.get_model("surveys", "Point")
    Measurement = apps.get_model("surveys", "Measurement")
    moved = kept = noted = 0
    for point in Point.objects.exclude(survey_mode=""):
        latest = (
            Measurement.objects.filter(point=point)
            .select_related("survey")
            .annotate(day=Coalesce("meas_date", "survey__date"))
            .order_by("-day", "-id")
            .first()
        )
        year = latest.survey.year if latest else None
        mode, history = split_survey_mode(point.survey_mode, year)
        if mode == "RTK" and year == 2026:
            mode = "RTK RADIO"
        if latest and mode:
            if (latest.meas_strategy or "").strip():
                kept += 1
            else:
                Measurement.objects.filter(pk=latest.pk).update(meas_strategy=mode)
                moved += 1
        note = history_note(history)
        notes = (point.notes or "").strip()
        notes = "" if notes in PLACEHOLDER_NOTES else notes
        if note and note not in notes:
            merged = f"{notes} {note}" if notes else note
            if len(merged) <= NOTES_MAX:
                Point.objects.filter(pk=point.pk).update(notes=merged)
                noted += 1
    print(
        f"\n  survey modes: {moved} moved to measurements, {kept} kept existing, "
        f"{noted} histories added to point notes"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0034_point_monograph_texts"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="measurement",
                    name="meas_strategy",
                    field=models.CharField(
                        blank=True,
                        db_comment=COLUMN_COMMENTS["meas_strategy"],
                        max_length=100,
                        null=True,
                        verbose_name="survey mode",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(sql=WIDEN, reverse_sql=migrations.RunSQL.noop),
            ],
        ),
        migrations.AlterField(
            model_name="measurement",
            name="notes",
            field=models.CharField(
                blank=True,
                db_comment=COLUMN_COMMENTS["notes"],
                max_length=250,
                null=True,
                verbose_name="measurement notes",
            ),
        ),
        migrations.RunPython(move_survey_mode, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="point",
            name="survey_mode",
        ),
    ]
