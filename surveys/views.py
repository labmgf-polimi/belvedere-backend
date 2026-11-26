from django.conf import settings
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from rest_framework.decorators import api_view
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from image_index.s3_utils import build_readonly_s3_client, generate_presigned_url

from .models import Measurement, MeasurementPhoto, PointMovement, Survey
from .serializers import MeasurementSerializer
from .sql import DAYS_PER_YEAR


@api_view(["GET"])
def survey_years(request: Request) -> Response:
    """List distinct years of surveys that have measurements, ascending."""
    years = (
        Survey.objects.filter(measurement__isnull=False)
        .order_by("year")
        .values_list("year", flat=True)
        .distinct()
    )
    return Response(list(years))


class MeasurementListView(ListAPIView):
    """List measurements with point/survey context.

    Query params:
        year: survey year (e.g. 2023)
        is_fixed: "true"/"false" — filter on the point's is_fixed flag
    """

    serializer_class = MeasurementSerializer
    pagination_class = None

    def get_queryset(self) -> QuerySet[Measurement]:
        qs = Measurement.objects.select_related("point", "survey").order_by(
            "point__label", "id"
        )
        year = self.request.query_params.get("year")
        if year:
            qs = qs.filter(survey__year=year)
        is_fixed = self.request.query_params.get("is_fixed", "").lower()
        if is_fixed in ("true", "false"):
            qs = qs.filter(point__is_fixed=(is_fixed == "true"))
        return qs


@api_view(["GET"])
def point_velocity(request: Request, label: str) -> Response:
    """Yearly horizontal velocity series of a point, in m/day.

    Read from the `point_movements` view (annual pairs only), which works in
    m/yr; the response keeps its original shape and m/day unit. Empty for
    unknown or fixed points (fixed points have no movements).
    """
    movements = PointMovement.objects.filter(label=label, is_annual=True).order_by(
        "date_fin"
    )
    return Response(
        [
            {
                "survey_year": m.survey_year,
                "survey_date_ini": m.date_prev,
                "survey_date_fin": m.date_fin,
                "v": m.v / DAYS_PER_YEAR,
            }
            for m in movements
        ]
    )


#: Lifetime of the presigned S3 link a photo URL redirects to, seconds.
PHOTO_LINK_SECONDS = 900


# TODO: public access — drop the staff check (and consider proxying the bytes)
# once the photos are meant to be visible outside the admin.
@staff_member_required
def measurement_photo(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Stable URL of a photo: redirects to a short-lived presigned S3 link.

    The bucket is private and presigned links expire, so this is the URL to
    link to or store; the browser then downloads straight from S3.
    """
    photo = get_object_or_404(MeasurementPhoto, pk=pk)
    url = generate_presigned_url(
        build_readonly_s3_client(),
        settings.S3_SURVEYS_BUCKET_NAME,
        photo.image.name,
        expires_in=PHOTO_LINK_SECONDS,
    )
    return redirect(url)


@staff_member_required
def admin_guide(request: HttpRequest) -> HttpResponse:
    """Database description and how-to, shown inside the admin."""
    context = {**admin.site.each_context(request), "title": "Guide: database and admin"}
    return render(request, "admin/surveys/guide.html", context)


def api_home(request: HttpRequest) -> HttpResponse:
    """Public landing page of the API: endpoints, fields, licence, links."""
    from image_index.models import Camera

    years = list(
        Survey.objects.filter(measurement__isnull=False)
        .order_by("year")
        .values_list("year", flat=True)
        .distinct()
    )
    # A moving point with yearly velocities, for the example link.
    example_point = (
        PointMovement.objects.filter(is_annual=True)
        .values_list("label", flat=True)
        .order_by("-survey_year", "label")
        .first()
    )
    camera = Camera.objects.filter(is_active=True).order_by("camera_name").first()
    context = {
        "latest_year": years[-1] if years else None,
        "first_year": years[0] if years else None,
        "example_point": example_point,
        "example_camera": camera.slug if camera else None,
    }
    return render(request, "surveys/api_home.html", context)
