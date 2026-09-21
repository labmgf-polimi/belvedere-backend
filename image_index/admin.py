import json

from django import forms
from django.contrib import admin
from django.contrib.gis import admin as gis_admin
from django.contrib.gis.forms.widgets import OSMWidget
from django.contrib.gis.geos import Point
from django.db.models import Count, Max, Min
from django.urls import reverse
from django.utils.html import format_html

from georef.enu import format_enu_point

from .models import Camera, CameraCalibration, Image

# ========== Cameras and Calibrations ==========


class CameraAdminForm(forms.ModelForm):
    class Meta:
        model = Camera
        fields = "__all__"
        widgets = {"location": OSMWidget(attrs={"map_width": 800, "map_height": 500})}


@admin.register(Camera)
class CameraAdmin(gis_admin.GISModelAdmin):
    form = CameraAdminForm
    prepopulated_fields = {
        "slug": ("camera_name",),
    }

    list_display = (
        "id",
        "camera_name",
        "slug",
        "notes",
        "model",
        "lens",
        "is_active",
        "installation_date",
        "image_count",
        "min_image_date",
        "max_image_date",
        "image_count_link",
    )
    search_fields = ("camera_name", "serial_number", "model", "lens", "notes")
    list_filter = (
        "camera_name",
        "model",
        "lens",
        "installation_date",
        "created_at",
    )
    readonly_fields = ("id", "created_at", "enu_coordinates")
    ordering = ("camera_name",)

    @admin.display(description="Local ENU (SRID 990001)")
    def enu_coordinates(self, obj) -> str:
        return format_enu_point(obj.location_enu)

    def save_model(self, request, obj, form, change):
        # OSMWidget always produces 2D points; the location column requires 3D.
        # Inject Z from elevation, falling back to 0 if not set.
        if obj.location is not None and not obj.location.hasz:
            z = obj.elevation if obj.elevation is not None else 0.0
            obj.location = Point(
                obj.location.x, obj.location.y, z, srid=obj.location.srid
            )
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            _image_count=Count("images"),
            _min_image_date=Min("images__datetime"),
            _max_image_date=Max("images__datetime"),
        )

    @admin.display(ordering="_image_count", description="images")
    def image_count(self, obj):
        return getattr(obj, "_image_count", 0)

    @admin.display(ordering="_min_image_date", description="min_date")
    def min_image_date(self, obj):
        value = getattr(obj, "_min_image_date", None)
        return value.strftime("%Y-%m-%d %H:%M") if value else "No images"

    @admin.display(ordering="_max_image_date", description="max_date")
    def max_image_date(self, obj):
        value = getattr(obj, "_max_image_date", None)
        return value.strftime("%Y-%m-%d %H:%M") if value else "No images"

    @admin.display(description="images link")
    def image_count_link(self, obj):
        count = getattr(obj, "_image_count", 0)
        if count > 0:
            url = reverse("admin:image_index_image_changelist")
            return format_html(
                '<a href="{}?camera__id__exact={}">{} images</a>',
                url,
                obj.pk,
                count,
            )
        return "0 images"

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        initial["s3_bucket"] = "belvedere-images"
        return initial


@admin.register(CameraCalibration)
class CameraCalibrationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "camera",
        "calibration_date",
        "model_name",
        "model_id",
        "image_width_px",
        "image_height_px",
        "is_active",
    )
    list_filter = (
        "camera",
        "is_active",
        "calibration_date",
        "model_id",
    )
    search_fields = (
        "camera__camera_name",
        "camera__serial_number",
        "model_name",
        "notes",
    )
    readonly_fields = ("id", "created_at")
    autocomplete_fields = ("camera",)
    ordering = ("-calibration_date", "-id")

    def save_model(self, request, obj, form, change):
        if obj.is_active:
            CameraCalibration.objects.filter(camera=obj.camera, is_active=True).exclude(
                pk=obj.pk
            ).update(is_active=False)
        super().save_model(request, obj, form, change)


# ========== Filters based on datetime ==========


class BaseDateFilter(admin.SimpleListFilter):
    date_field = None

    @classmethod
    def create(cls, date_field):
        return type(
            f"{cls.__name__}_{date_field.replace('__', '_')}",
            (cls,),
            {"date_field": date_field},
        )


class YearFilterBase(BaseDateFilter):
    title = "year"
    parameter_name = "year"

    def lookups(self, request, model_admin):
        years = (
            model_admin.model.objects
            .exclude(**{f"{self.date_field}__isnull": True})
            .dates(self.date_field, "year")
            .values_list(f"{self.date_field}__year", flat=True)
        )
        return [(str(year), str(year)) for year in sorted(set(years), reverse=True)]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(**{f"{self.date_field}__year": self.value()})
        return queryset


class MonthFilterBase(BaseDateFilter):
    title = "month"
    parameter_name = "month"

    def lookups(self, request, model_admin):
        return (
            ("1", "January"),
            ("2", "February"),
            ("3", "March"),
            ("4", "April"),
            ("5", "May"),
            ("6", "June"),
            ("7", "July"),
            ("8", "August"),
            ("9", "September"),
            ("10", "October"),
            ("11", "November"),
            ("12", "December"),
        )

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(**{f"{self.date_field}__month": self.value()})
        return queryset


class DayFilterBase(BaseDateFilter):
    title = "day"
    parameter_name = "day"

    def lookups(self, request, model_admin):
        return [(str(i), str(i)) for i in range(1, 32)]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(**{f"{self.date_field}__day": self.value()})
        return queryset


class TimeOfDayFilterBase(BaseDateFilter):
    title = "time of day"
    parameter_name = "time_of_day"

    def lookups(self, request, model_admin):
        return tuple((str(i), f"{i:02d}:00 - {i + 1:02d}:00") for i in range(24))

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(**{f"{self.date_field}__hour": int(self.value())})
        return queryset


ImageYearFilter = YearFilterBase.create("datetime")
ImageMonthFilter = MonthFilterBase.create("datetime")
ImageDayFilter = DayFilterBase.create("datetime")
ImageTimeOfDayFilter = TimeOfDayFilterBase.create("datetime")


# ========== Image helpers ==========


# FILE_SIZE_CLASSES_MB = {
#     "large": (8, None),
#     "medium": (5, 8),
#     "small": (3, 5),
#     "tiny": (0, 3),
# }

# FILE_SIZE_COLORMAP = {
#     "large": "#090",
#     "medium": "#f90",
#     "small": "#d00",
#     "tiny": "#999",
# }


# def get_file_size_color(size_mb):
#     if size_mb is None:
#         return "#999"
#     for cls, (low, high) in FILE_SIZE_CLASSES_MB.items():
#         if size_mb >= low and (high is None or size_mb < high):
#             return FILE_SIZE_COLORMAP[cls]
#     return "#999"


# class FileSizeFilter(admin.SimpleListFilter):
#     title = "file size"
#     parameter_name = "file_size"

#     def lookups(self, request, model_admin):
#         return (
#             ("large", "≥ 8 MB"),
#             ("medium", "5–8 MB"),
#             ("small", "3–5 MB"),
#             ("tiny", "< 3 MB"),
#             ("missing", "Missing"),
#         )

#     def queryset(self, request, queryset):
#         mb = 1024 * 1024
#         val = self.value()

#         if val == "missing":
#             return queryset.filter(file_size_bytes__isnull=True)

#         if val in FILE_SIZE_CLASSES_MB:
#             low_mb, high_mb = FILE_SIZE_CLASSES_MB[val]
#             qs = queryset.filter(file_size_bytes__isnull=False).filter(
#                 file_size_bytes__gte=int(low_mb * mb)
#             )
#             if high_mb is not None:
#                 qs = qs.filter(file_size_bytes__lt=int(high_mb * mb))
#             return qs


#         return queryset


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        # "admin_thumbnail",
        "camera",
        "datetime",
        "filename",
        "label",
        "bucket",
        "rotation",
        "width_px",
        "height_px",
        "view_image",
    )
    list_filter = (
        "camera",
        "label",
        ImageYearFilter,
        ImageMonthFilter,
        # ImageDayFilter,
        ImageTimeOfDayFilter,
    )
    search_fields = (
        "id",
        "camera__camera_name",
        "filename",
        "label",
    )
    # date_hierarchy = "datetime"
    readonly_fields = (
        "id",
        "indexed_at",
        "updated_at",
        "file_size_bytes",
        "formatted_exif_data",
        "file_path",
        "image_preview",
    )
    autocomplete_fields = ("camera",)
    ordering = ("-datetime", "-id")
    list_select_related = ("camera",)
    show_full_result_count = False

    fields = (
        "id",
        "camera",
        "datetime",
        "filename",
        "label",
        "bucket",
        "object_key",
        "file_path",
        "image_preview",
        "rotation",
        "width_px",
        "height_px",
        "file_size_bytes",
        "formatted_exif_data",
        "indexed_at",
        "updated_at",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("camera")

    @admin.display(description="EXIF data")
    def formatted_exif_data(self, obj):
        if not obj.exif_data:
            return "No EXIF data"

        try:
            formatted_json = json.dumps(
                obj.exif_data, indent=2, ensure_ascii=False, sort_keys=True
            )
            return format_html(
                '<pre style="background:#f8f8f8; padding:10px; border:1px solid #ddd; '
                "border-radius:4px; font-family:monospace; font-size:12px; "
                'max-height:400px; overflow-y:auto;">{}</pre>',
                formatted_json,
            )
        except (TypeError, ValueError):
            return format_html(
                '<pre style="background:#fff2f2; padding:10px; border:1px solid #fdd; '
                'border-radius:4px; color:#d00;">Invalid JSON data</pre>'
            )

    @admin.display(description="Preview")
    def image_preview(self, obj):
        if not obj or not obj.pk:
            return "Save the image first to see a preview"

        url = reverse("image_index:serve_image_preview", args=[obj.pk])
        return format_html(
            '<img src="{}" alt="preview" loading="lazy" decoding="async" '
            'style="max-width:420px; max-height:280px; border:1px solid #ddd; border-radius:6px;" />',
            url,
        )

    # @admin.display(description="Thumb")
    # def admin_thumbnail(self, obj):
    #     if not obj or not obj.pk:
    #         return "-"

    #     url = reverse("image_index:serve_image_thumbnail", args=[obj.pk])
    #     return format_html(
    #         '<img src="{}" alt="thumbnail" loading="lazy" decoding="async" '
    #         'style="width:80px; height:56px; object-fit:cover; '
    #         'border:1px solid #ddd; border-radius:4px;" />',
    #         url,
    #     )

    @admin.display(description="View image")
    def view_image(self, obj):
        url = reverse("image_index:serve_image", args=[obj.pk])
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">View Image</a>',
            url,
        )
