"""CSV export of points with their latest measurement (Points admin button)."""

import csv
import io

from surveys.models import Measurement, Point, latest_measurement
from surveys.monographs import geocentric
from surveys.utils.photos import FIELD_TIME_ZONE

POINT_COLUMNS = [
    "label", "active", "is_fixed", "ref_date", "target", "description", "point_notes",
]  # fmt: skip
MEASUREMENT_COLUMNS = [
    "survey_id", "survey_year", "survey_date", "meas_date", "meas_time_local",
    "survey_mode", "east_7791", "north_7791", "h_ellipsoidal",
    "h_orthometric_italgeo05", "lat_4326", "lon_4326", "std_east", "std_north",
    "std_h", "x_6704", "y_6704", "z_6704",
    # Local frame: U is height above the tangent plane at D12 + 1000, not an altitude.
    "enu_e_990001", "enu_n_990001", "enu_u_990001_not_altitude",
    "datum_realization", "height_type", "measurement_notes", "photos", "measurement_id",
]  # fmt: skip


def _num(value: float | None, decimals: int) -> str:
    return "" if value is None else f"{value:.{decimals}f}"


def _measurement_values(m: Measurement) -> list:
    """Coordinates at 0.1 mm (metric) and 1e-9 degree (~0.1 mm) resolution."""
    x, y, z = geocentric(m.east, m.north, m.h)
    enu = m.geom_enu
    local = m.meas_time.astimezone(FIELD_TIME_ZONE) if m.meas_time else None
    return [
        m.survey_id,
        m.survey.year,
        m.survey.date,
        m.meas_date or "",
        f"{local:%Y-%m-%d %H:%M:%S %Z}" if local else "",
        m.meas_strategy or "",
        _num(m.east, 4),
        _num(m.north, 4),
        _num(m.h, 4),
        _num(m.h_orto, 4),
        _num(m.lat, 9),
        _num(m.lon, 9),
        _num(m.std_east, 4),
        _num(m.std_north, 4),
        _num(m.std_h, 4),
        _num(x, 4),
        _num(y, 4),
        _num(z, 4),
        _num(enu.x if enu else None, 4),
        _num(enu.y if enu else None, 4),
        _num(enu.z if enu else None, 4),
        m.datum_realization or "",
        m.height_type or "",
        m.notes or "",
        m.photos.count(),
        m.pk,
    ]


def latest_measurements_csv(points: list[Point]) -> str:
    """One row per point; measurement columns empty for never-measured points."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(POINT_COLUMNS + MEASUREMENT_COLUMNS)
    for p in points:
        m = latest_measurement(p)
        point_values = [
            p.label, p.active, p.is_fixed, p.ref_date or "", p.target, p.description,
            p.notes or "",
        ]  # fmt: skip
        tail = _measurement_values(m) if m else [""] * len(MEASUREMENT_COLUMNS)
        writer.writerow(point_values + tail)
    return out.getvalue()
