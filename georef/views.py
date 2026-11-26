"""Public API of the reference systems used by the project's data."""

from functools import cache

import pyproj
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from georef.constants import (
    GEOCENTRIC_SRID,
    GEOGRAPHIC_2D_SRID,
    PROJECT_SRID,
)
from georef.models import ReferenceFrame

#: EPSG code -> what it is used for in this project.
CRS_USES = {
    PROJECT_SRID: "Project CRS: east and north of every measurement, metres.",
    GEOGRAPHIC_2D_SRID: "Geographic latitude/longitude of the same realization (RDN2008).",
    4326: (
        "Latitude/longitude as served by the API. Numerically identical to "
        f"EPSG:{GEOGRAPHIC_2D_SRID} (null transformation); today's WGS 84 / ITRF "
        "differs from ETRF2000 by about 0.45 m."
    ),
    GEOCENTRIC_SRID: "Geocentric Cartesian X, Y, Z (monographs, CSV export), metres.",
}

HEIGHTS = [
    {"field": "h", "type": "ellipsoidal", "reference": "GRS80 ellipsoid", "unit": "m"},
    {
        "field": "h_orto",
        "type": "orthometric",
        "reference": "ITALGEO05 geoid",
        "unit": "m",
    },
]

FRAME_NOTE = (
    "Transform with proj_pipeline (forward: geographic -> local, inverse: back). "
    "Up is the height above the tangent plane at the origin plus false_origin.up: "
    "not an altitude. Do not reproject local coordinates with a generic CRS definition."
)


@cache
def _crs_name(epsg: int) -> str:
    return pyproj.CRS.from_epsg(epsg).name


def _frame(frame: ReferenceFrame) -> dict:
    return {
        "srid": frame.srid,
        "name": frame.name,
        "description": frame.description,
        "origin_mark": frame.origin_mark,
        "origin": {"lat": frame.lat_0, "lon": frame.lon_0, "h_ellipsoidal": frame.h_0},
        "ellipsoid": frame.ellps,
        "false_origin": {"east": frame.x_off, "north": frame.y_off, "up": frame.z_off},
        "base_crs": f"EPSG:{frame.base_srid}",
        "datum_epoch": float(frame.datum_epoch)
        if frame.datum_epoch is not None
        else None,
        "valid_from": frame.valid_from,
        "frozen": frame.frozen,
        "proj_pipeline": frame.proj_pipeline,
        "pipeline_input": "longitude, latitude (degrees), ellipsoidal height (m)",
        "pipeline_output": "east, north, up (m)",
        "note": FRAME_NOTE,
    }


@api_view(["GET"])
def reference_systems(request: Request) -> Response:
    """The CRSs, height references and local frames the data is expressed in."""
    return Response(
        {
            "project_crs": f"EPSG:{PROJECT_SRID}",
            "crs": [
                {"code": f"EPSG:{epsg}", "name": _crs_name(epsg), "use": use}
                for epsg, use in CRS_USES.items()
            ],
            "heights": HEIGHTS,
            "local_frames": [
                _frame(f) for f in ReferenceFrame.objects.order_by("srid")
            ],
        }
    )
