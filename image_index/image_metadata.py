import re
from datetime import datetime as dt
from io import BytesIO

from django.utils import timezone
from PIL import Image as PILImage
from PIL.ExifTags import TAGS
from PIL.TiffImagePlugin import IFDRational

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp")

# Filename datetime patterns, tried in order — first match wins. Each must
# define the same named groups (y, mo, d, h, mi, s) so they share one parser.
FILENAME_DATETIME_PATTERNS: list[re.Pattern] = [
    # Standard filenames like cam01_20260901_070000.jpg, where the date is a contiguous YYYYMMDD block.
    re.compile(
        r"^[^_]+_(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})_"
        r"(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})_.*\.(jpg|jpeg|png|tif|tiff|webp)$",
        re.IGNORECASE,
    ),
    # Vendor filenames like cam01_canon_rgb_2026_09_01_070000.jpg, where the
    # date is underscore-separated instead of a contiguous YYYYMMDD block.
    re.compile(
        r"^.+_(?P<y>\d{4})_(?P<mo>\d{2})_(?P<d>\d{2})_"
        r"(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})\.(jpg|jpeg|png|tif|tiff|webp)$",
        re.IGNORECASE,
    ),
]


def make_aware_if_needed(value):
    if value is None:
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value


def parse_datetime_from_filename(filename):
    filename = filename or ""

    for pattern in FILENAME_DATETIME_PATTERNS:
        match = pattern.match(filename)
        if not match:
            continue

        g = match.groupdict()
        try:
            return make_aware_if_needed(
                dt.strptime(
                    f"{g['y']}{g['mo']}{g['d']}{g['h']}{g['mi']}{g['s']}",
                    "%Y%m%d%H%M%S",
                )
            )
        except ValueError:
            return None

    return None


def parse_datetime_from_exif_dict(exif_data):
    if not exif_data or not isinstance(exif_data, dict):
        return None

    for key in [
        "DateTimeOriginal",
        "EXIF DateTimeOriginal",
        "DateTimeDigitized",
        "EXIF DateTimeDigitized",
        "DateTime",
        "Image DateTime",
    ]:
        value = exif_data.get(key)
        if not value:
            continue

        if isinstance(value, (list, tuple)) and value:
            value = value[0]

        if not isinstance(value, str):
            continue

        value = value.strip()
        try:
            parsed = dt.strptime(value, "%Y:%m:%d %H:%M:%S")
            return make_aware_if_needed(parsed)
        except ValueError:
            continue

    return None


def make_json_safe(value):
    """Recursively convert Pillow EXIF types to JSON-serializable Python types."""
    if isinstance(value, IFDRational):
        try:
            return float(value)
        except Exception:
            return str(value)

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="ignore")
        except Exception:
            return str(value)

    if isinstance(value, dict):
        return {str(make_json_safe(k)): make_json_safe(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [make_json_safe(v) for v in value]

    if isinstance(value, set):
        return [make_json_safe(v) for v in sorted(value, key=str)]

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    return str(value)


def extract_exif_data_from_bytes(image_bytes):
    with BytesIO(image_bytes) as bio:
        with PILImage.open(bio) as img:
            exif_raw = img.getexif()
            if not exif_raw:
                return None

            exif_data = {}
            for tag_id, value in exif_raw.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                exif_data[tag_name] = make_json_safe(value)

            return exif_data or None


def extract_image_metadata_from_bytes(image_bytes, filename=None, mime_type=None):
    with BytesIO(image_bytes) as bio:
        with PILImage.open(bio) as img:
            width_px, height_px = img.size
            exif_raw = img.getexif()

            exif_data = {}
            if exif_raw:
                for tag_id, value in exif_raw.items():
                    tag_name = TAGS.get(tag_id, str(tag_id))
                    exif_data[tag_name] = make_json_safe(value)

            exif_data = exif_data or None

            image_datetime = parse_datetime_from_exif_dict(exif_data)
            if image_datetime is None:
                image_datetime = parse_datetime_from_filename(filename)

            orientation = (exif_data or {}).get("Orientation") or (exif_data or {}).get(
                "Image Orientation"
            )
            orientation_map = {
                1: 0,
                3: 180,
                6: 90,
                8: 270,
                "Horizontal (normal)": 0,
                "Rotated 90 CW": 90,
                "Rotated 180": 180,
                "Rotated 90 CCW": 270,
                "Rotated 270 CW": 270,
            }

            return {
                "datetime": image_datetime,
                "width_px": width_px,
                "height_px": height_px,
                "mime_type": mime_type,
                "exif_data": exif_data,
                "rotation": orientation_map.get(orientation, 0),
            }
