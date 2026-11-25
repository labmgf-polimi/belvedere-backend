import re
import tempfile
from datetime import date
from pathlib import Path

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.views.main import IS_POPUP_VAR
from django.contrib.gis import admin as gis_admin
from django.contrib.gis.forms.widgets import OSMWidget
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.db.models import Count, Max, Min, OuterRef, QuerySet, Subquery
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html
from import_export.admin import ImportExportMixin

from georef.enu import format_enu_point

from .exports import latest_measurements_csv
from .models import (
    Flight,
    Instrument,
    Measurement,
    MeasurementPhoto,
    Point,
    PointMovement,
    Product2D,
    Product3D,
    Survey,
    SurveyHasInstrument,
    Volume,
)
from .monographs import natural_key, prepare, render_html, write_pdf
from .resources import MeasurementResource
from .sql import COLUMN_COMMENTS as DOC
from .sql import POINT_MOVEMENTS_COLUMNS, POINT_MOVEMENTS_COMMENTS
from .utils.photos import FIELD_TIME_ZONE


def changelist_geojson(
    queryset: QuerySet, fields: dict[str, str], url_name: str, limit: int
) -> dict:
    """GeoJSON FeatureCollection of `queryset` for the admin changelist map.

    Args:
        queryset: Rows to plot.
        fields: Feature property -> ORM lookup. Must contain `lat` and `lon`
            (WGS 84, computed in the database). With `lat_prev`/`lon_prev` too,
            each feature is a line from the earlier to that position. The
            remaining keys become popup properties (`label`, `year`, `date`, ...).
        url_name: Admin URL name each feature links to, reversed with the row pk.
        limit: Maximum number of features.
    """
    coords = {"lat", "lon", "lat_prev", "lon_prev"}
    props = {k: v for k, v in fields.items() if k not in coords}
    not_null = {f"{fields[k]}__isnull": False for k in coords if k in fields}
    rows = queryset.filter(**not_null).order_by().values("pk", *fields.values())[:limit]

    def geometry(r: dict) -> dict:
        end = [r[fields["lon"]], r[fields["lat"]]]
        if "lat_prev" not in fields:
            return {"type": "Point", "coordinates": end}
        start = [r[fields["lon_prev"]], r[fields["lat_prev"]]]
        return {"type": "LineString", "coordinates": [start, end]}

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": geometry(r),
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
    #: Wording of the map summary line ("Map — <n> <noun>").
    map_noun: str = "point(s), following the current filters and search"
    #: Colour by value classes instead of by year: {"property", "title", "unit",
    #: "classes": [{"label", "max" (exclusive, None = open), "color"}]}.
    map_classes: dict | None = None

    def map_rows(
        self, request: HttpRequest, queryset: QuerySet
    ) -> tuple[QuerySet, str]:
        """Rows to plot (from the filtered changelist queryset) and the caption."""
        return queryset, self.map_noun

    def changelist_view(
        self, request: HttpRequest, extra_context: dict | None = None
    ) -> HttpResponse:
        response = super().changelist_view(request, extra_context)
        # Redirects and error pages carry no changelist.
        context = getattr(response, "context_data", None)
        if context and "cl" in context:
            rows, context["map_noun"] = self.map_rows(request, context["cl"].queryset)
            context["map_geojson"] = changelist_geojson(
                rows, self.map_fields, self.map_url_name, self.map_max_features
            )
            context["map_classes"] = self.map_classes
        return response


#: Horizontal velocity classes, m/yr, for the movement map. Round breaks that
#: split the annual velocities (0-31 m/yr, median 6 in 2015-2026) into
#: comparable groups. Colours: matplotlib's "magma" reversed, sampled at 1.0,
#: 0.82, 0.64, 0.45, 0.25 — perceptually uniform, near-white (slow) -> dark
#: purple (fast), stopping short of black so fast dots stay visible on dark
#: rock; adjacent classes stay apart for colour-blind readers (worst protan
#: dE 11.0, dataviz palette validator). The map rings dots in dark grey so the
#: near-white end reads on bright debris.
VELOCITY_CLASSES = {
    "property": "v",
    "title": "Horizontal velocity",
    "unit": "m/yr",
    "classes": [
        {"label": "< 2", "max": 2, "color": "#fcfdbf"},
        {"label": "2–5", "max": 5, "color": "#fea973"},
        {"label": "5–10", "max": 10, "color": "#ea5661"},
        {"label": "10–20", "max": 20, "color": "#a1307e"},
        {"label": "≥ 20", "max": None, "color": "#51127c"},
    ],
}


def rounded_columns(model: type[models.Model], *fields: str, digits: int = 4):
    """Class decorator adding `<field>_rounded` list columns.

    Each shows a float field to `digits` decimals, keeps its header, sorting and
    hover description. Display only: the database keeps full precision.
    (A plain method named like the field would be ignored — Django resolves
    model fields first.)
    """

    def decorate(cls: type) -> type:
        for field in fields:

            def column(self, obj, field: str = field) -> str | None:
                value = getattr(obj, field)
                return None if value is None else f"{value:.{digits}f}"

            label = model._meta.get_field(field).verbose_name
            name = f"{field}_rounded"
            setattr(cls, name, admin.display(ordering=field, description=label)(column))
            if field in getattr(cls, "field_help", {}):
                cls.field_help = cls.field_help | {name: cls.field_help[field]}
        return cls

    return decorate


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
    #: Path from the admin's model to MeasurementPhoto.
    photos_lookup = "photos"

    def lookups(self, request: HttpRequest, model_admin) -> list[tuple[str, str]]:
        return [("yes", "With photos"), ("no", "Without photos")]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        isnull = {"yes": False, "no": True}.get(self.value())
        if isnull is None:
            return queryset
        return queryset.filter(**{f"{self.photos_lookup}__isnull": isnull}).distinct()


class PointHasPhotosFilter(HasPhotosFilter):
    photos_lookup = "measurement__photos"


class PointStatusFilter(admin.SimpleListFilter):
    """Active points by default; the inactive ones (history) on request.

    The point picker popup (raw_id lookups from the Measurement form) shows
    all points by default, so old measurements can still be assigned.
    """

    title = "status"
    parameter_name = "status"

    def __init__(self, request: HttpRequest, *args, **kwargs) -> None:
        super().__init__(request, *args, **kwargs)
        self.default = "all" if IS_POPUP_VAR in request.GET else "active"

    def lookups(self, request: HttpRequest, model_admin) -> list[tuple[str, str]]:
        return [
            ("active", "Active"),
            ("inactive", "Inactive (history)"),
            ("all", "All points"),
        ]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        status = self.value() or self.default
        if status == "active":
            return queryset.filter(active=True)
        if status == "inactive":
            return queryset.exclude(active=True)  # also points with no flag
        return queryset

    def choices(self, changelist):
        current = self.value() or self.default
        for value, label in self.lookup_choices:
            yield {
                "selected": value == current,
                "query_string": changelist.get_query_string(
                    {self.parameter_name: value}
                ),
                "display": label,
            }


class LastMeasuredFilter(admin.SimpleListFilter):
    """Points by the year of their latest measurement (PointAdmin annotation)."""

    title = "last measured in"
    parameter_name = "last_measured"

    def lookups(self, request: HttpRequest, model_admin) -> list[tuple[str, str]]:
        years = (
            Survey.objects.filter(measurement__isnull=False)
            .order_by("-year")
            .values_list("year", flat=True)
            .distinct()
        )
        return [(str(year), str(year)) for year in years]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        if self.value():
            return queryset.filter(_last_year=self.value())
        return queryset


class MeasurementCountFilter(admin.SimpleListFilter):
    """Points by number of measurements (PointAdmin annotation)."""

    title = "number of measurements"
    parameter_name = "measurements"
    RANGES: dict[str, tuple[int, int | None]] = {
        "0": (0, 0),
        "1": (1, 1),
        "2-5": (2, 5),
        "6+": (6, None),
    }

    def lookups(self, request: HttpRequest, model_admin) -> list[tuple[str, str]]:
        return [
            ("0", "None"),
            ("1", "Only one"),
            ("2-5", "2 to 5"),
            ("6+", "6 or more"),
        ]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        if self.value() not in self.RANGES:
            return queryset
        low, high = self.RANGES[self.value()]
        queryset = queryset.filter(_num_measurements__gte=low)
        return (
            queryset if high is None else queryset.filter(_num_measurements__lte=high)
        )


class FieldHelpMixin:
    """Hover descriptions on list columns, inline columns and form labels.

    Texts are each model field's `db_comment` (the description QGIS shows) or
    `help_text`, so they are written once; `field_help` adds entries for
    computed columns or overrides.
    """

    change_form_template = "admin/surveys/change_form_help.html"
    field_help: dict[str, str] = {}

    def get_field_help(self) -> dict[str, str]:
        models = [self.model, *(inline.model for inline in self.inlines)]
        texts: dict[str, str] = {}
        for model in reversed(models):  # the admin's own model wins on clashes
            for f in model._meta.concrete_fields:
                if f.db_comment or f.help_text:
                    texts[f.name] = str(f.db_comment or f.help_text)
        return texts | self.field_help

    def changelist_view(
        self, request: HttpRequest, extra_context: dict | None = None
    ) -> HttpResponse:
        context = {**(extra_context or {}), "field_help": self.get_field_help()}
        return super().changelist_view(request, context)

    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict | None = None,
    ) -> HttpResponse:
        context = {**(extra_context or {}), "field_help": self.get_field_help()}
        return super().changeform_view(request, object_id, form_url, context)


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
class PointAdmin(FieldHelpMixin, ChangeListMapMixin, admin.ModelAdmin):
    # Add the monograph buttons to the list and the edit form.
    change_list_template = "admin/surveys/point/change_list.html"
    change_form_template = "admin/surveys/point/change_form.html"
    field_help = {
        "monograph_links": "Open the point's monograph, or download it as PDF.",
        "first_survey_date": "Date of the first survey that measured this point.",
        "last_survey_date": "Date of the most recent survey that measured this point.",
        "last_east": "Easting at the latest measurement, m, EPSG:7791.",
        "last_north": "Northing at the latest measurement, m, EPSG:7791.",
        "last_h": "Ellipsoidal height at the latest measurement, m.",
        "num_measurements": "Number of measurements of this point.",
        "measurements_link": "Open this point's measurements.",
    }
    # One marker per point at its latest measurement, coloured by that survey's year.
    map_fields = {
        "lat": "_last_lat",
        "lon": "_last_lon",
        "label": "label",
        "year": "_last_year",
        "date": "_last_survey_date",
        "h": "_last_h",
    }
    map_url_name = "admin:surveys_point_change"

    list_display = (
        "id",
        "label",
        "active",
        "is_fixed",
        "ref_date",
        "first_survey_date",
        "last_survey_date",
        "last_east",
        "last_north",
        "last_h",
        "num_measurements",
        "measurements_link",
        "monograph_links",
        "notes",
        "created_at",
    )
    search_fields = ("id", "label", "notes")
    list_filter = (
        PointStatusFilter,
        "is_fixed",
        (
            "measurement__survey__year",
            titled_filter(admin.AllValuesFieldListFilter, "measured in"),
        ),
        LastMeasuredFilter,
        MeasurementCountFilter,
        PointHasPhotosFilter,
        "ref_date",
    )
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

    def changelist_view(
        self, request: HttpRequest, extra_context: dict | None = None
    ) -> HttpResponse:
        status = request.GET.get(PointStatusFilter.parameter_name)
        if (
            request.method == "GET"
            and IS_POPUP_VAR not in request.GET
            and status in (None, "active")
        ):
            hidden = Point.objects.exclude(active=True).count()
            self.message_user(
                request,
                f"Showing active points only: {hidden} inactive points (the history "
                'of the glacier\'s movement) are hidden. Choose "All points" or '
                '"Inactive (history)" under Status on the right to see them.',
                messages.WARNING,
            )
        return super().changelist_view(request, extra_context)

    # --- monographs (see surveys.monographs) ------------------------------

    def get_urls(self) -> list:
        view = self.admin_site.admin_view
        return [
            path("monographs/", view(self.monographs_view), name="surveys_point_monographs"),
            path("monographs.pdf", view(self.monographs_view), {"pdf": True},
                 name="surveys_point_monographs_pdf"),
            path("export-latest.csv", view(self.export_latest_view),
                 name="surveys_point_export_latest"),
            path("<path:object_id>/monograph/", view(self.monograph_view),
                 name="surveys_point_monograph"),
            path("<path:object_id>/monograph.pdf", view(self.monograph_view), {"pdf": True},
                 name="surveys_point_monograph_pdf"),
        ] + super().get_urls()  # fmt: skip

    def export_latest_view(self, request: HttpRequest) -> HttpResponse:
        """CSV of the listed points (same filters) with their latest measurement."""
        if not self.has_view_permission(request):
            raise PermissionDenied
        points = sorted(
            self.get_changelist_instance(request).queryset,
            key=lambda p: natural_key(p.label),
        )
        # BOM: Excel then reads the file as UTF-8 (accented Italian texts).
        response = HttpResponse(
            "\ufeff" + latest_measurements_csv(points),
            content_type="text/csv; charset=utf-8",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="points_latest_measurement_{date.today():%Y%m%d}.csv"'
        )
        return response

    def monograph_view(
        self, request: HttpRequest, object_id: str, pdf: bool = False
    ) -> HttpResponse:
        point = self.get_object(request, object_id)
        if point is None:
            raise Http404("No such point")
        pdf_url = reverse("admin:surveys_point_monograph_pdf", args=[point.pk])
        name = "monograph_" + re.sub(r"\W+", "_", point.label)
        return self._monographs(request, [point], pdf, False, name, pdf_url)

    def monographs_view(self, request: HttpRequest, pdf: bool = False) -> HttpResponse:
        """All the points the list shows with the same filters and search."""
        points = sorted(
            self.get_changelist_instance(request).queryset,
            key=lambda p: natural_key(p.label),
        )
        query = request.GET.urlencode()
        pdf_url = reverse("admin:surveys_point_monographs_pdf") + (
            f"?{query}" if query else ""
        )
        return self._monographs(request, points, pdf, True, "monographs", pdf_url)

    def _monographs(
        self,
        request: HttpRequest,
        points: list[Point],
        pdf: bool,
        cover: bool,
        name: str,
        pdf_url: str,
    ) -> HttpResponse:
        if not self.has_view_permission(request):
            raise PermissionDenied
        with tempfile.TemporaryDirectory() as tmp:
            mset = prepare(
                points, None, Path(tmp), settings.MONOGRAPH_TILE_CACHE, photo_files=pdf
            )
            if not mset.monographs:
                self.message_user(
                    request, "No measured point to show.", messages.WARNING
                )
                return redirect("admin:surveys_point_changelist")
            if not pdf:
                html = render_html(
                    mset, None, cover=cover, for_pdf=False, pdf_url=pdf_url
                )
                return HttpResponse(html)
            data, _ = write_pdf(render_html(mset, None, cover=cover))
        response = HttpResponse(data, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'attachment; filename="{name}_{date.today():%Y%m%d}.pdf"'
        )
        return response

    @admin.display(description="Monograph")
    def monograph_links(self, obj: Point) -> str:
        return format_html(
            '<a href="{}" target="_blank">View</a> · <a href="{}">PDF</a>',
            reverse("admin:surveys_point_monograph", args=[obj.pk]),
            reverse("admin:surveys_point_monograph_pdf", args=[obj.pk]),
        )

    def get_queryset(self, request: HttpRequest) -> QuerySet[Point]:
        # Latest measurement = most recent survey date, as in the active_points view.
        latest = Measurement.objects.filter(point=OuterRef("pk")).order_by(
            "-survey__date", "-id"
        )
        return (
            super()
            .get_queryset(request)
            .annotate(
                _first_survey_date=Min("measurement__survey__date"),
                _last_survey_date=Max("measurement__survey__date"),
                _num_measurements=Count("measurement"),
                **{
                    f"_last_{field}": Subquery(latest.values(lookup)[:1])
                    for field, lookup in (
                        ("east", "east"),
                        ("north", "north"),
                        ("h", "h"),
                        ("lat", "lat"),
                        ("lon", "lon"),
                        ("year", "survey__year"),
                    )
                },
            )
        )

    @admin.display(ordering="_last_east", description="Last east")
    def last_east(self, obj: Point) -> float | None:
        return getattr(obj, "_last_east", None)

    @admin.display(ordering="_last_north", description="Last north")
    def last_north(self, obj: Point) -> float | None:
        return getattr(obj, "_last_north", None)

    @admin.display(ordering="_last_h", description="Last h")
    def last_h(self, obj: Point) -> float | None:
        return getattr(obj, "_last_h", None)

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


@admin.register(Measurement)
class MeasurementAdmin(
    FieldHelpMixin, ChangeListMapMixin, ImportExportMixin, gis_admin.GISModelAdmin
):
    resource_classes = [MeasurementResource]
    import_template_name = "admin/surveys/import_measurement.html"
    gis_widget = MapWidget
    # import-export wraps the mixin's change_list_template, so its buttons stay.
    field_help = {
        "point": "Monitoring point that was measured.",
        "survey": "Survey campaign the measurement belongs to.",
        "label": DOC["label"],
        "survey_year": DOC["survey_year"],
        "survey_date": DOC["survey_date"],
        "is_fixed": DOC["is_fixed"],
        "is_active": DOC["is_active"],
        "photo_count": "Number of photos of this measurement.",
        "enu_coordinates": DOC["geom_enu"],
        "measured_at": "When the point was measured, local time (Europe/Rome).",
        "point_target": DOC["target"],
        "point_description": DOC["description"],
        "point_notes": "Notes on the point (warnings, state of the target).",
    }

    # Point and survey columns come through the foreign keys (list_select_related),
    # so this page covers everything the points_measurements view showed.
    list_display = (
        "id",
        "label",
        "survey_year",
        "survey_date",
        "meas_date",
        "measured_at",
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
    readonly_fields = (
        "created_at",
        "enu_coordinates",
        "measured_at",
        "point_target",
        "point_description",
        "point_notes",
        "point_link",
    )

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

    @admin.display(ordering="meas_time", description="Measured at (local)")
    def measured_at(self, obj: Measurement) -> str:
        if obj.meas_time is None:
            return "-"
        return f"{obj.meas_time.astimezone(FIELD_TIME_ZONE):%Y-%m-%d %H:%M:%S %Z}"

    # The point's monograph texts, read-only here: they belong to the point,
    # not to one measurement (edit them on the point).
    @admin.display(description="Target")
    def point_target(self, obj: Measurement) -> str:
        return obj.point.target or "-"

    @admin.display(description="Description")
    def point_description(self, obj: Measurement) -> str:
        return obj.point.description or "-"

    @admin.display(description="Point notes")
    def point_notes(self, obj: Measurement) -> str:
        return obj.point.notes or "-"

    @admin.display(description="Edit")
    def point_link(self, obj: Measurement) -> str:
        url = reverse("admin:surveys_point_change", args=[obj.point_id])
        return format_html(
            '<a href="{}">Edit these texts on point {}</a>', url, obj.point.label
        )

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "point",
                    "survey",
                    "meas_date",
                    "meas_time",
                    "measured_at",
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
            "Point (monograph)",
            {
                "fields": (
                    "point_target",
                    "point_description",
                    "point_notes",
                    "point_link",
                ),
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


@admin.register(PointMovement)
@rounded_columns(PointMovement, "d", "v", "a", "v_h", "d_3d", "v_3d")
class PointMovementAdmin(FieldHelpMixin, ChangeListMapMixin, ReadOnlyViewAdmin):
    """Movements of the moving points.

    The map draws every annual movement as a bar from its start to its end
    position at true scale, coloured by that year's horizontal velocity: a
    stake's consecutive years join into its path, showing how its velocity
    changes along the way.
    """

    map_fields = {
        "lat_prev": "lat_prev",
        "lon_prev": "lon_prev",
        "lat": "lat_fin",
        "lon": "lon_fin",
        "label": "label",
        "year": "survey_year",
        "date_prev": "date_prev",
        "date": "date_fin",
        "v": "v",
        "d": "d",
    }
    # Links to the later measurement of the pair (the view's id).
    map_url_name = "admin:surveys_measurement_change"
    map_classes = VELOCITY_CLASSES
    map_noun = (
        "annual movements, coloured by horizontal velocity "
        "(all years unless filtered; intra-annual pairs are not drawn)"
    )

    def map_rows(
        self, request: HttpRequest, queryset: QuerySet
    ) -> tuple[QuerySet, str]:
        # Yearly velocities only come from annual pairs.
        return queryset.filter(is_annual=True), self.map_noun

    field_help = {
        column: DOC[column] for column in POINT_MOVEMENTS_COLUMNS
    } | POINT_MOVEMENTS_COMMENTS

    list_display = (
        "label",
        "survey_year",
        "date_prev",
        "date_fin",
        "dt",
        "is_annual",
        "d_rounded",
        "v_rounded",
        "a_rounded",
        "v_h_rounded",
        "d_3d_rounded",
        "v_3d_rounded",
    )
    search_fields = ("label",)
    list_filter = (
        "is_annual",
        (
            "survey_year",
            titled_filter(admin.AllValuesFieldListFilter, "year (end of movement)"),
        ),
    )
    ordering = ("label", "-date_fin")
