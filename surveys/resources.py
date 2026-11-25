import logging
from datetime import datetime

from django.contrib.gis.geos import Point as GEOSPoint
from django.core.exceptions import ValidationError
from import_export import fields, resources
from import_export.widgets import DateWidget

from georef.constants import PROJECT_SRID

from .models import Measurement, Point, Survey

logger = logging.getLogger(__name__)


class MeasurementResource(resources.ModelResource):
    meas_date = fields.Field(
        column_name="meas_date",
        attribute="meas_date",
        widget=DateWidget(format="%Y-%m-%d"),
    )
    point_label = fields.Field(column_name="point_label")
    survey_date = fields.Field(column_name="survey_date")

    class Meta:
        model = Measurement
        fields = (
            "id",
            "point",
            "point_label",
            "survey",
            "survey_date",
            "east",
            "north",
            "h",
            "meas_date",
            "notes",
            "std_east",
            "std_north",
            "std_h",
            "meas_strategy",
            "lat",
            "lon",
            "h_orto",
        )
        import_id_fields = ()
        skip_unchanged = False
        report_skipped = True

    def dehydrate_point_label(self, obj: Measurement) -> str:
        return obj.point.label or ""

    def dehydrate_survey_date(self, obj: Measurement) -> str:
        d = obj.survey.date
        return d.strftime("%d/%m/%Y") if d else ""

    def before_import(self, dataset, **kwargs):
        self.created_points = []

    def before_import_row(self, row, **kwargs):
        point_label = (row.get("point_label") or "").strip()
        survey_date = (row.get("survey_date") or "").strip()

        if not point_label:
            raise ValidationError("Missing required column/value: point_label")

        survey_id_raw = (row.get("survey_id") or "").strip()

        if not survey_id_raw and not survey_date:
            raise ValidationError(
                "Missing required column: provide 'survey_id' or 'survey_date'."
            )

        if row.get("east") in (None, ""):
            raise ValidationError("Missing required column/value: east")

        if row.get("north") in (None, ""):
            raise ValidationError("Missing required column/value: north")

        if row.get("h") in (None, ""):
            raise ValidationError("Missing required column/value: h")

        # Case-insensitive: an exact-match lookup created D01BIS alongside the
        # existing D01bis in the 2026 import, splitting that stake's history in
        # two and losing its displacement in point_movements.
        point = Point.objects.filter(label__iexact=point_label).first()
        created = point is None
        if created:
            point = Point.objects.create(
                label=point_label,
                active=True,
                is_fixed=False,
                notes="Automatically created during measurement CSV import",
            )

        if created:
            msg = f"Point automatically created from import: label='{point_label}', id={point.pk}"
            self.created_points.append(msg)
            logger.warning(msg)

        if survey_id_raw:
            try:
                survey = Survey.objects.get(pk=int(survey_id_raw))
            except (ValueError, TypeError):
                raise ValidationError(
                    f"Invalid survey_id '{survey_id_raw}': must be an integer."
                ) from None
            except Survey.DoesNotExist:
                raise ValidationError(
                    f"Survey not found for id='{survey_id_raw}'."
                ) from None
        else:
            try:
                survey_date_parsed = datetime.strptime(survey_date, "%d/%m/%Y").date()
            except ValueError:
                raise ValidationError(
                    f"Invalid date format '{survey_date}'. Expected DD/MM/YYYY."
                ) from None
            try:
                survey = Survey.objects.get(date=survey_date_parsed)
            except Survey.DoesNotExist:
                raise ValidationError(
                    f"Survey not found for date='{survey_date}'."
                ) from None
            except Survey.MultipleObjectsReturned:
                raise ValidationError(
                    f"Multiple surveys found for date='{survey_date}'. Use survey_id instead."
                ) from None

        row["point"] = point.pk
        row["survey"] = survey.pk

        row["notes"] = row.get("notes") or ""
        # Renamed from ds_* in August 2026; older CSV templates still work.
        for axis in ("east", "north", "h"):
            row[f"std_{axis}"] = row.get(f"std_{axis}") or row.get(f"ds_{axis}") or None
        row["meas_strategy"] = row.get("meas_strategy") or ""
        row["lat"] = row.get("lat") or None
        row["lon"] = row.get("lon") or None
        row["h_orto"] = row.get("h_orto") or None

    def before_save_instance(self, instance, row, **kwargs):
        if instance.east is not None and instance.north is not None:
            instance.geom = GEOSPoint(
                float(instance.east), float(instance.north), srid=PROJECT_SRID
            )

    def after_import(self, dataset, result, **kwargs):
        if self.created_points:
            logger.warning(
                f"Measurement import completed with {len(self.created_points)} auto-created points:\n{chr(10).join(self.created_points)}",
            )
