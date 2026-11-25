"""Say what the dates of a movement refer to, in the column comments QGIS shows.

`survey_year` is the year of the later (end) measurement; `date_prev` /
`date_fin` are the start / end of the movement. (The admin labels are Django
state on an unmanaged model, which needs no migration.)
"""

from django.db import migrations

from surveys.sql import POINT_MOVEMENTS_COMMENTS, comment_columns


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0031_point_movements_view"),
    ]

    operations = [
        migrations.RunSQL(
            sql=comment_columns(
                "point_movements",
                ("survey_year", "date_prev", "date_fin"),
                overrides=POINT_MOVEMENTS_COMMENTS,
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
