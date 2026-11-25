from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from rest_framework.decorators import api_view
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from image_index.s3_utils import build_readonly_s3_client, generate_presigned_url

from .models import Measurement, MeasurementPhoto, Survey
from .serializers import MeasurementSerializer
from .utils.velocity import compute_point_velocities


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
    """Yearly velocity series for a point, like the points_movement_filtered view.

    Returns an empty list for unknown or fixed points (legacy behaviour).
    """
    measurements = list(
        Measurement.objects.filter(point__label=label, point__is_fixed=False)
        .select_related("survey")
        .order_by("survey__date")
    )
    return Response(compute_point_velocities(measurements))


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
