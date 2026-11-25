from django.contrib import admin, messages
from django.contrib.gis import admin as gis_admin
from django.contrib.gis.forms.widgets import OSMWidget
from django.db import transaction
from django.db.models import Count, Max, Min, QuerySet
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.html import format_html
from import_export.admin import ImportExportMixin

from georef.enu import format_enu_point

from .models import (
    ActivePoint,
    Flight,
    Instrument,
    Measurement,
    MeasurementPhoto,
    Point,
    PointsMovementFiltered,
    PointsMovementRaw,
    Product2D,
    Product3D,
    Survey,
    SurveyHasInstrument,
    Volume,
)
from .resources import MeasurementResource


@admin.register(Survey)
class SurveyAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "year", "notes")
    search_fields = ("id", "year", "notes")
    list_filter = ("year", "date")
    ordering = ("-year", "-date")


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "type", "brand", "year_of_m", "reseller")
    search_fields = ("id", "name", "type", "brand", "reseller")
    list_filter = ("type", "brand", "year_of_m")
    ordering = ("name",)


@admin.register(SurveyHasInstrument)
class SurveyHasInstrumentAdmin(admin.ModelAdmin):
    list_display = ("id", "fk_surveys", "fk_instrum")
    search_fields = ("id", "fk_surveys__id", "fk_instrum__name", "fk_instrum__brand")
    list_filter = ("fk_surveys", "fk_instrum")
    raw_id_fields = ("fk_surveys", "fk_instrum")
    ordering = ("id",)


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "fk_surveys",
        "camera_name",
        "n_images",
        "average_height",
        "average_gsd",
        "global_accuracy",
    )
    search_fields = ("id", "camera_name", "fk_surveys__id")
    list_filter = ("fk_surveys", "camera_name")
    raw_id_fields = ("fk_surveys",)
    ordering = ("id",)


@admin.register(Point)
class PointAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "label",
        "active",
        "is_fixed",
        "ref_date",
        "first_survey_date",
        "last_survey_date",
        "num_measurements",
        "measurements_link",
        "notes",
        "created_at",
    )
    search_fields = ("id", "label", "notes")
    list_filter = ("active", "is_fixed", "ref_date")
    ordering = ("label", "id")
    readonly_fields = (
        "created_at",
        "first_survey_date",
        "last_survey_date",
        "num_measurements",
        "measurements_link",
    )
    actions = ("delete_with_measurements",)

    @admin.action(
        description="Delete selected points AND all their measurements",
        permissions=["delete"],
    )
    def delete_with_measurements(self, request: HttpRequest, queryset):
        """Deliberate counterpart to the PROTECT on `Measurement.point`.

        The plain delete refuses while measurements exist, which is what keeps
        a stray click from destroying survey data. This is the explicit way
        through, and it reports exactly how much was removed.
        """
        measurements = Measurement.objects.filter(point__in=queryset)
        measurement_count = measurements.count()
        labels = list(queryset.values_list("label", flat=True))

        with transaction.atomic():
            measurements.delete()
            point_count, _ = queryset.delete()

        self.message_user(
            request,
            f"Deleted {point_count} point(s) and {measurement_count} measurement(s): "
            f"{', '.join(labels)}.",
            messages.WARNING,
        )

    def get_queryset(self, request: HttpRequest) -> QuerySet[Point]:
        return (
            super()
            .get_queryset(request)
            .annotate(
                _first_survey_date=Min("measurement__survey__date"),
                _last_survey_date=Max("measurement__survey__date"),
                _num_measurements=Count("measurement"),
            )
        )

    @admin.display(ordering="_first_survey_date", description="First survey date")
    def first_survey_date(self, obj: Point):
        return getattr(obj, "_first_survey_date", None)

    @admin.display(ordering="_last_survey_date", description="Last survey date")
    def last_survey_date(self, obj: Point):
        return getattr(obj, "_last_survey_date", None)

    @admin.display(ordering="_num_measurements", description="Measurements")
    def num_measurements(self, obj: Point):
        return getattr(obj, "_num_measurements", None)

    @admin.display(description="Measurements")
    def measurements_link(self, obj: Point):
        url = reverse("admin:surveys_measurement_changelist")
        return format_html(
            '<a href="{}?point__id__exact={}">View measurements</a>',
            url,
            obj.pk,
        )


class MapWidget(OSMWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attrs.update({"map_width": 800, "map_height": 500})


def changelist_geojson(
    queryset: QuerySet, fields: dict[str, str], url_name: str, limit: int
) -> dict:
    """GeoJSON FeatureCollection of `queryset` for the admin changelist map.

    Args:
        queryset: Rows to plot.
        fields: Feature property -> ORM lookup. Must contain `lat` and `lon`
            (WGS 84, computed in the database); the remaining keys become
            popup properties (`label`, `year`, `date`, `h`).
        url_name: Admin URL name each feature links to, reversed with the row pk.
        limit: Maximum number of features.
    """
    lat, lon = fields["lat"], fields["lon"]
    props = {k: v for k, v in fields.items() if k not in ("lat", "lon")}
    rows = (
        queryset.filter(**{f"{lat}__isnull": False, f"{lon}__isnull": False})
        .order_by()
        .values("pk", *fields.values())[:limit]
    )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r[lon], r[lat]]},
                "properties": {
                    **{k: r[v] for k, v in props.items()},
                    "url": reverse(url_name, args=[r["pk"]]),
                },
            }
            for r in rows
        ],
    }


class ChangeListMapMixin:
    """Adds a collapsible Leaflet map of every filtered changelist row.

    The map follows the changelist filters and search but not pagination.
    Coordinates must be WGS 84 `lat`/`lon` columns filled by the database —
    never an ENU geometry, which cannot be reprojected correctly.
    """

    change_list_template = "admin/surveys/change_list_map.html"
    map_fields: dict[str, str] = {
        "lat": "lat",
        "lon": "lon",
        "label": "point__label",
        "year": "survey__year",
        "date": "meas_date",
        "h": "h",
    }
    map_url_name: str = "admin:surveys_measurement_change"
    map_max_features: int = 5000

    def changelist_view(
        self, request: HttpRequest, extra_context: dict | None = None
    ) -> HttpResponse:
        response = super().changelist_view(request, extra_context)
        # Redirects and error pages carry no changelist.
        context = getattr(response, "context_data", None)
        if context and "cl" in context:
            context["map_geojson"] = changelist_geojson(
                context["cl"].queryset,
                self.map_fields,
                self.map_url_name,
                self.map_max_features,
            )
        return response


def photo_preview(photo: MeasurementPhoto | None) -> str:
    """Thumbnail linking to the full photo, both via the stable photo URL."""
    if not photo or not photo.pk or not photo.image:
        return "-"
    url = photo.get_absolute_url()
    # Full-size file scaled by the browser; server-side thumbnails are a TODO.
    return format_html(
        '<a href="{}" target="_blank" rel="noopener noreferrer">'
        '<img src="{}" alt="{}" loading="lazy" '
        'style="max-height:160px; max-width:240px; border:1px solid #ccc;"></a>',
        url,
        url,
        photo.file_name,
    )


class MeasurementPhotoInline(admin.TabularInline):
    """Add photos in the empty rows, replace by choosing a new file, remove with Delete."""

    model = MeasurementPhoto
    extra = 1
    fields = ("image", "preview", "file_name", "uploaded_at")
    readonly_fields = ("preview", "file_name", "uploaded_at")

    @admin.display(description="Preview")
    def preview(self, obj: MeasurementPhoto) -> str:
        return photo_preview(obj)


def titled_filter(base: type[admin.FieldListFilter], title: str) -> type:
    """`base` with a custom sidebar title (Django uses the field name)."""

    class TitledFilter(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.title = title

    return TitledFilter


class HasPhotosFilter(admin.SimpleListFilter):
    title = "photos"
    parameter_name = "has_photos"

    def lookups(self, request: HttpRequest, model_admin) -> list[tuple[str, str]]:
        return [("yes", "With photos"), ("no", "Without photos")]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        if self.value() == "yes":
            return queryset.filter(photos__isnull=False).distinct()
        if self.value() == "no":
            return queryset.filter(photos__isnull=True)
        return queryset


@admin.register(Measurement)
class MeasurementAdmin(ChangeListMapMixin, ImportExportMixin, gis_admin.GISModelAdmin):
    resource_classes = [MeasurementResource]
    import_template_name = "admin/surveys/import_measurement.html"
    gis_widget = MapWidget
    # import-export wraps the mixin's change_list_template, so its buttons stay.

    # Point and survey columns come through the foreign keys (list_select_related),
    # so this page covers everything the points_measurements view showed.
    list_display = (
        "id",
        "label",
        "survey_year",
        "survey_date",
        "meas_date",
        "east",
        "north",
        "h",
        "h_orto",
        "is_fixed",
        "is_active",
        "meas_strategy",
        "photo_count",
        "created_at",
    )
    list_select_related = ("point", "survey")
    search_fields = (
        "id",
        "point__label",
        "point__id",
        "survey__id",
        "notes",
        "meas_strategy",
    )
    list_filter = (
        ("point__is_fixed", titled_filter(admin.BooleanFieldListFilter, "fixed point")),
        ("point__active", titled_filter(admin.BooleanFieldListFilter, "active point")),
        ("survey__year", titled_filter(admin.AllValuesFieldListFilter, "survey year")),
        "survey",
        "meas_date",
        "meas_strategy",
        HasPhotosFilter,
    )
    inlines = [MeasurementPhotoInline]
    raw_id_fields = ("point", "survey")
    ordering = ("-survey__year", "point__label", "-meas_date")
    readonly_fields = ("created_at", "enu_coordinates")

    @admin.display(description="SRID 990001")
    def enu_coordinates(self, obj) -> str:
        return format_enu_point(obj.geom_enu)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Measurement]:
        return super().get_queryset(request).annotate(_photo_count=Count("photos"))

    @admin.display(ordering="_photo_count", description="Photos")
    def photo_count(self, obj: Measurement) -> int:
        return getattr(obj, "_photo_count", 0)

    @admin.display(ordering="point__label", description="Label")
    def label(self, obj: Measurement) -> str:
        return obj.point.label

    @admin.display(ordering="survey__year", description="Survey year")
    def survey_year(self, obj: Measurement) -> int | None:
        return obj.survey.year

    @admin.display(ordering="survey__date", description="Survey date")
    def survey_date(self, obj: Measurement):
        return obj.survey.date

    @admin.display(boolean=True, ordering="point__is_fixed", description="Fixed")
    def is_fixed(self, obj: Measurement) -> bool | None:
        return obj.point.is_fixed

    @admin.display(boolean=True, ordering="point__active", description="Active")
    def is_active(self, obj: Measurement) -> bool | None:
        return obj.point.active

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "point",
                    "survey",
                    "meas_date",
                    "meas_strategy",
                    "east",
                    "north",
                    "h",
                    "h_orto",
                    "lat",
                    "lon",
                    "std_east",
                    "std_north",
                    "std_h",
                    "notes",
                    "created_at",
                )
            },
        ),
        (
            "Map",
            {"fields": ("geom",)},
        ),
        (
            "Local ENU coordinates",
            {
                "fields": ("enu_coordinates",),
                "description": "The ENU position is derived from east/north/h by a database trigger; edit the source coordinates to change it.",
            },
        ),
    )


@admin.register(MeasurementPhoto)
class MeasurementPhotoAdmin(admin.ModelAdmin):
    list_display = ("id", "measurement", "file_name", "uploaded_at", "preview")
    list_select_related = ("measurement",)
    search_fields = ("id", "measurement__id", "measurement__point__label", "file_name")
    list_filter = ("uploaded_at", "measurement__survey")
    raw_id_fields = ("measurement",)
    fields = (
        "measurement",
        "image",
        "preview",
        "file_name",
        "uploaded_at",
        "image_key",
    )
    readonly_fields = ("preview", "file_name", "uploaded_at", "image_key")
    ordering = ("-uploaded_at", "-id")

    @admin.display(description="Preview")
    def preview(self, obj: MeasurementPhoto) -> str:
        return photo_preview(obj)

    @admin.display(description="S3 key")
    def image_key(self, obj: MeasurementPhoto) -> str:
        return obj.image.name if obj.image else "-"


class BaseProductAdmin(admin.ModelAdmin):
    search_fields = ("id", "survey__id", "data_type")
    list_filter = ("data_type", "survey")
    raw_id_fields = ("survey",)
    ordering = ("survey__year", "id")


@admin.register(Product2D)
class Product2DAdmin(BaseProductAdmin):
    list_display = ("id", "survey", "data_type", "file_format", "wms_url")


@admin.register(Product3D)
class Product3DAdmin(BaseProductAdmin):
    list_display = ("id", "survey", "data_type", "file_format", "url")


@admin.register(Volume)
class VolumeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "survey_prev",
        "survey",
        "dv",
        "density",
        "density_uncertainty",
    )
    raw_id_fields = ("survey", "survey_prev")
    ordering = ("survey__year",)


class ReadOnlyViewAdmin(admin.ModelAdmin):
    """Browse-only admin for the unmanaged models mapped onto Postgres views."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


class PointsMovementAdmin(ReadOnlyViewAdmin):
    list_display = (
        "label",
        "survey_year",
        "survey_date_prev",
        "survey_date_fin",
        "dt",
        "d",
        "v",
        "a",
    )
    search_fields = ("label",)
    list_filter = ("survey_year", "is_fixed")
    ordering = ("label", "-survey_date_fin")


@admin.register(PointsMovementRaw)
class PointsMovementRawAdmin(PointsMovementAdmin):
    pass


@admin.register(PointsMovementFiltered)
class PointsMovementFilteredAdmin(PointsMovementAdmin):
    pass


@admin.register(ActivePoint)
class ActivePointAdmin(ReadOnlyViewAdmin):
    list_display = (
        "label",
        "last_measure_date",
        "east",
        "north",
        "h",
        "is_fixed",
    )
    search_fields = ("label",)
    list_filter = ("is_fixed",)
    ordering = ("label",)
