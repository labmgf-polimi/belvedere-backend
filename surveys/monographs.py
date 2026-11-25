"""Point monographs: one A4 page per point, built from the database only.

Each page shows the point's latest measurement up to a given year: survey date
and mode, the point's texts, an overview map, its coordinates in three systems
and up to two photos (empty boxes when there are none). See
`manage.py generate_monographs`.
"""

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from django.contrib.staticfiles import finders
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.template.loader import render_to_string
from django.templatetags.static import static
from PIL import Image, ImageOps
from pyproj import CRS, Transformer

from georef.constants import GEOCENTRIC_SRID, PROJECT_SRID
from surveys.models import Measurement, MeasurementPhoto, Point, latest_measurement
from surveys.utils.basemap import ATTRIBUTION, Basemap, build_basemap
from surveys.utils.photos import FIELD_TIME_ZONE

logger = logging.getLogger(__name__)

MAX_PHOTOS = 2
#: Longest side of a photo in the PDF; the originals in S3 are untouched.
PHOTO_PX = 1200


@lru_cache(maxsize=1)
def _to_geocentric() -> Transformer:
    return Transformer.from_crs(
        CRS.from_epsg(PROJECT_SRID).to_3d(),
        CRS.from_epsg(GEOCENTRIC_SRID),
        always_xy=True,
    )


def geocentric(east: float, north: float, h: float) -> tuple[float, float, float]:
    """RDN2008 geocentric X, Y, Z (m) from UTM 32N east/north and ellipsoidal h."""
    return _to_geocentric().transform(east, north, h)


def dms(value: float, positive: str, negative: str, decimals: int = 4) -> str:
    """Degrees, minutes, seconds: 45.950736 -> 45° 57′ 02.6496″ N.

    Rounded in whole units of the last decimal of the seconds, so 59.99999″
    carries into the minutes instead of printing 60″.
    """
    scale = 10**decimals
    total = round(abs(value) * 3600 * scale)
    degrees, rest = divmod(total, 3600 * scale)
    minutes, seconds = divmod(rest, 60 * scale)
    hemisphere = positive if value >= 0 else negative
    return f"{degrees}° {minutes:02d}′ {seconds / scale:0{3 + decimals}.{decimals}f}″ {hemisphere}"


def _metres(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f} m"


def _sigma(value: float | None) -> str:
    return "" if value is None else f"± {value:.3f}"


def coordinate_blocks(m: Measurement) -> list[dict]:
    """The three coordinate blocks of a page, formatted."""
    x, y, z = geocentric(m.east, m.north, m.h)
    return [
        {
            "title": "RDN2008 / UTM zone 32N",
            "code": "EPSG:7791",
            "rows": [
                ("East", _metres(m.east), _sigma(m.std_east)),
                ("North", _metres(m.north), _sigma(m.std_north)),
                ("H orthometric", _metres(m.h_orto), ""),
            ],
        },
        {
            "title": "WGS 84 geographic",
            "code": "EPSG:4326",
            "rows": [
                # Each in degrees-minutes-seconds and decimal degrees
                # (0.0001″ and 1e-9° are both well below a millimetre).
                ("Latitude", dms(m.lat, "N", "S"), ""),
                ("", f"{m.lat:.9f}°", ""),
                ("Longitude", dms(m.lon, "E", "W"), ""),
                ("", f"{m.lon:.9f}°", ""),
                ("h ellipsoidal", _metres(m.h), _sigma(m.std_h)),
            ],
        },
        {
            "title": "Geocentric Cartesian (RDN2008)",
            "code": "EPSG:6704",
            "rows": [
                ("X", _metres(x), ""),
                ("Y", _metres(y), ""),
                ("Z", _metres(z), ""),
            ],
        },
    ]


@dataclass
class Monograph:
    point: Point
    measurement: Measurement
    #: Up to MAX_PHOTOS, most recent first.
    photos: list[MeasurementPhoto]
    #: Their upright, downscaled local copies (PDF only).
    files: list[Path] = field(default_factory=list)

    @property
    def measured_at(self) -> str:
        m = self.measurement
        if m.meas_time:
            return f"{m.meas_time.astimezone(FIELD_TIME_ZONE):%Y-%m-%d %H:%M %Z}"
        return f"{m.meas_date or m.survey.date}"

    @property
    def anchor(self) -> str:
        return "p-" + re.sub(r"\W+", "-", self.point.label)


def natural_key(label: str) -> list:
    """D2 before D10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", label)]


def select_points(labels: list[str] | None, include_inactive: bool) -> list[Point]:
    points = Point.objects.exclude(label=None)
    if labels:
        query = Q()
        for label in labels:
            query |= Q(label__iexact=label)
        points = points.filter(query)
    elif not include_inactive:
        points = points.filter(active=True)
    return sorted(points, key=lambda p: natural_key(p.label))


def _photo_file(photo: MeasurementPhoto, folder: Path) -> Path | None:
    """The photo upright and downscaled, as a local JPEG; None if unreadable."""
    try:
        with photo.image.open("rb") as stored:
            image = ImageOps.exif_transpose(Image.open(BytesIO(stored.read())))
    except Exception:  # missing object, network error: leave the box empty
        logger.warning("photo %s (%s) not readable", photo.pk, photo.image.name)
        return None
    image.thumbnail((PHOTO_PX, PHOTO_PX))
    path = folder / f"photo_{photo.pk}.jpg"
    image.convert("RGB").save(path, quality=85)
    return path


def collect(
    point: Point, until_year: int | None, folder: Path | None
) -> Monograph | None:
    """The point's monograph data, or None when it has no measurement up to the year.

    Photos are downloaded into `folder` (for the PDF) only when one is given.
    """
    measurement = latest_measurement(point, until_year)
    if measurement is None or measurement.lat is None:
        return None
    photos = MeasurementPhoto.objects.filter(measurement__point=point)
    if until_year is not None:
        photos = photos.filter(measurement__survey__year__lte=until_year)
    photos = list(
        photos.annotate(
            day=Coalesce("measurement__meas_date", "measurement__survey__date")
        ).order_by("-day", "-uploaded_at", "-id")[:MAX_PHOTOS]
    )
    files = (
        [] if folder is None else [f for p in photos if (f := _photo_file(p, folder))]
    )
    return Monograph(point, measurement, photos, files)


def network_positions(until_year: int | None) -> list[tuple[float, float]]:
    """(lon, lat) of the active points' latest measurements: the network on every map."""
    positions = []
    for point in Point.objects.filter(active=True):
        m = latest_measurement(point, until_year)
        if m is not None and m.lat is not None:
            positions.append((m.lon, m.lat))
    return positions


@dataclass
class MonographSet:
    monographs: list[Monograph]
    #: Labels of the points without a measurement up to the year.
    skipped: list[str]
    network: list[tuple[float, float]]
    basemap: Basemap


def prepare(
    points: list[Point],
    until_year: int | None,
    folder: Path,
    tile_cache: Path,
    photo_files: bool,
) -> MonographSet:
    """Collect the points and build the overview map (in `folder`)."""
    monographs, skipped = [], []
    for point in points:
        m = collect(point, until_year, folder if photo_files else None)
        (monographs if m else skipped).append(m or point.label)
    network = network_positions(until_year) + [
        (m.measurement.lon, m.measurement.lat) for m in monographs
    ]
    basemap = build_basemap(network, folder / "overview.jpg", tile_cache)
    return MonographSet(monographs, skipped, network, basemap)


def _file_uri(path: str) -> str:
    found = finders.find(path)
    if not found:
        raise FileNotFoundError(path)
    return Path(found).as_uri()


def _triangle(x: float, y: float, size: float = 0.9) -> str:
    """SVG polygon points of a small upward triangle centred on x, y."""
    return f"{x:.3f},{y - size:.3f} {x - 0.85 * size:.3f},{y + 0.6 * size:.3f} {x + 0.85 * size:.3f},{y + 0.6 * size:.3f}"


def render_html(
    mset: MonographSet,
    until_year: int | None,
    cover: bool = True,
    for_pdf: bool = True,
    pdf_url: str = "",
    monographs: list[Monograph] | None = None,
) -> str:
    """The monographs as one HTML document.

    Args:
        for_pdf: Local files for photos and logos (WeasyPrint); otherwise
            browser URLs (stable photo URLs, static files).
        pdf_url: Shown as a download link on screen (browser only).
        monographs: A subset of `mset.monographs` (e.g. one point per file).
    """
    basemap = mset.basemap
    ratio = basemap.height_px / basemap.width_px

    def marker(lon: float, lat: float) -> tuple[float, float]:
        # Overlay units: the SVG viewBox is 100 wide and 100 * ratio high.
        x, y = basemap.position(lon, lat)
        return x, y * ratio

    pages = []
    for m in monographs if monographs is not None else mset.monographs:
        x, y = marker(m.measurement.lon, m.measurement.lat)
        photos = (
            [f.as_uri() for f in m.files] if for_pdf
            else [p.get_absolute_url() for p in m.photos]
        )  # fmt: skip
        pages.append(
            {
                "m": m,
                "blocks": coordinate_blocks(m.measurement),
                "photos": photos + [None] * (MAX_PHOTOS - len(photos)),
                "marker": {"x": f"{x:.3f}", "y": f"{y:.3f}",
                           "lx": f"{x + 3:.3f}", "ly": f"{y - 2:.3f}"},
            }
        )  # fmt: skip
    scale_m, scale_pct = basemap.scale_bar()
    height = 100 * ratio
    logo = _file_uri if for_pdf else static
    # Embedded once (in the stylesheet) and reused by every page's map.
    overview = (
        "data:image/jpeg;base64," + base64.b64encode(basemap.path.read_bytes()).decode()
    )
    return render_to_string(
        "surveys/monograph.html",
        {
            "pages": pages,
            "cover": cover,
            "network": [_triangle(*marker(lon, lat)) for lon, lat in mset.network],
            "map_height": f"{height:.3f}",
            "scale_y": f"{height - 4:.3f}",
            "scale_pct": f"{scale_pct:.3f}",
            "scale_m": scale_m,
            "overview": overview,
            "attribution": ATTRIBUTION,
            "until_year": until_year,
            "generated": date.today(),
            "logo_polimi": logo("surveys/monograph/polimi.jpg"),
            "logo_labmgf": logo("surveys/monograph/labmgf.png"),
            "pdf_url": pdf_url,
        },
    )


def write_pdf(html: str, out: Path | None = None) -> tuple[bytes, int]:
    """PDF bytes and page count of `html`; also written to `out` if given."""
    from weasyprint import HTML  # heavy, and only needed here

    document = HTML(string=html, base_url=".").render()
    data = document.write_pdf()
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    return data, len(document.pages)
