"""Satellite overview image for the monographs (Esri World Imagery).

The tiles are Web Mercator (EPSG:3857). Points are placed with pyproj; the
tile grid itself is the standard XYZ scheme, i.e. a linear subdivision of the
EPSG:3857 square. Tiles are cached on disk, so re-runs fetch nothing.
"""

import math
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image
from pyproj import Geod, Transformer

TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer"
    "/tile/{z}/{y}/{x}"
)
ATTRIBUTION = "Imagery: Esri, Maxar, Earthstar Geographics, and the GIS User Community"
TILE_PX = 256
#: Half the side of the EPSG:3857 square, metres.
MERCATOR_HALF = 20037508.342789244
MAX_ZOOM = 17


@lru_cache(maxsize=1)
def _to_mercator() -> Transformer:
    return Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


def to_mercator(lon: float, lat: float) -> tuple[float, float]:
    return _to_mercator().transform(lon, lat)


@dataclass(frozen=True)
class Basemap:
    path: Path
    #: xmin, ymin, xmax, ymax in EPSG:3857 metres.
    bounds: tuple[float, float, float, float]
    width_px: int
    height_px: int
    #: Ground metres per pixel along the image's middle row.
    metres_per_px: float

    def position(self, lon: float, lat: float) -> tuple[float, float]:
        """Where a point falls, as percent of the image width and height."""
        x, y = to_mercator(lon, lat)
        xmin, ymin, xmax, ymax = self.bounds
        return 100 * (x - xmin) / (xmax - xmin), 100 * (ymax - y) / (ymax - ymin)

    def scale_bar(self, fraction: float = 0.2) -> tuple[int, float]:
        """A round length (m) near `fraction` of the width, and its width in percent."""
        target = fraction * self.width_px * self.metres_per_px
        step = 10 ** math.floor(math.log10(target))
        length = max(m * step for m in (1, 2, 2.5, 5) if m * step <= target)
        return int(length), 100 * length / (self.width_px * self.metres_per_px)


def _extent(
    lonlats: list[tuple[float, float]], aspect: float, margin: float, min_span_m: float
) -> tuple[float, float, float, float]:
    """Mercator box around the points, padded and widened to `aspect` (w/h)."""
    xs, ys = zip(*(to_mercator(lon, lat) for lon, lat in lonlats), strict=True)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    w = max(max(xs) - min(xs), min_span_m) * (1 + 2 * margin)
    h = max(max(ys) - min(ys), min_span_m) * (1 + 2 * margin)
    w, h = max(w, h * aspect), max(h, w / aspect)
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def _tile(z: int, x: int, y: int, cache_dir: Path) -> Image.Image:
    path = cache_dir / str(z) / str(x) / f"{y}.jpg"
    if not path.exists():
        request = urllib.request.Request(
            TILE_URL.format(z=z, x=x, y=y),
            headers={"User-Agent": "belvedere-backend-monographs"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return Image.open(BytesIO(path.read_bytes())).convert("RGB")


def build_basemap(
    lonlats: list[tuple[float, float]],
    out: Path,
    cache_dir: Path,
    width_px: int = 1800,
    aspect: float = 4 / 3,
    margin: float = 0.08,
    min_span_m: float = 400.0,
) -> Basemap:
    """Satellite image covering all `lonlats` (lon, lat in degrees), saved to `out`."""
    xmin, ymin, xmax, ymax = _extent(lonlats, aspect, margin, min_span_m)
    world = 2 * MERCATOR_HALF
    # Smallest zoom whose native resolution is at least `width_px` over the box.
    zoom = min(
        MAX_ZOOM, math.ceil(math.log2(width_px * world / (TILE_PX * (xmax - xmin))))
    )
    tile_m = world / 2**zoom
    tx0, tx1 = (int((v + MERCATOR_HALF) // tile_m) for v in (xmin, xmax))
    ty0, ty1 = (int((MERCATOR_HALF - v) // tile_m) for v in (ymax, ymin))

    mosaic = Image.new("RGB", ((tx1 - tx0 + 1) * TILE_PX, (ty1 - ty0 + 1) * TILE_PX))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            mosaic.paste(
                _tile(zoom, tx, ty, cache_dir),
                ((tx - tx0) * TILE_PX, (ty - ty0) * TILE_PX),
            )

    px_m = tile_m / TILE_PX
    left = (xmin + MERCATOR_HALF - tx0 * tile_m) / px_m
    top = (MERCATOR_HALF - ymax - ty0 * tile_m) / px_m
    crop = mosaic.crop(
        (
            round(left),
            round(top),
            round(left + (xmax - xmin) / px_m),
            round(top + (ymax - ymin) / px_m),
        )
    )
    height_px = round(width_px / aspect)
    crop.resize((width_px, height_px), Image.LANCZOS).save(out, quality=88)

    # Ground width across the middle of the image (Mercator stretches with latitude).
    inverse = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    mid = (ymin + ymax) / 2
    (lon0, lat0), (lon1, lat1) = (inverse.transform(x, mid) for x in (xmin, xmax))
    ground_width = Geod(ellps="WGS84").inv(lon0, lat0, lon1, lat1)[2]
    return Basemap(
        out, (xmin, ymin, xmax, ymax), width_px, height_px, ground_width / width_px
    )
