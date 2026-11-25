"""Field photos of the measured points: names, orientation, review sheets.

The GNSS controller names each photo `Img_<LABEL>_<DDMMYY>_<HHMMSS>.jpg` in
local time and writes no EXIF date, so the name is the only timestamp.
Its camera sets the EXIF orientation only sometimes; untagged photos can still
be sideways, which only a human can tell — hence explicit extra rotations.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageOps

PHOTO_NAME = re.compile(
    r"^Img_(?P<label>.+)_(?P<date>\d{6})_(?P<time>\d{6})\.jpe?g$", re.IGNORECASE
)
JPEG_QUALITY = 95
#: Time zone of the field work and of the controller's clock.
FIELD_TIME_ZONE = ZoneInfo("Europe/Rome")


@dataclass(frozen=True)
class FieldPhoto:
    path: Path
    label: str
    taken_at: datetime  # timezone-aware

    @property
    def name(self) -> str:
        return self.path.name


def parse_photo_name(path: Path, tz: ZoneInfo) -> FieldPhoto | None:
    """`Img_D01BIS_270726_091105.jpg` -> label D01BIS, 2026-07-27 09:11:05 in `tz`."""
    match = PHOTO_NAME.match(path.name)
    if not match:
        return None
    local = datetime.strptime(match["date"] + match["time"], "%d%m%y%H%M%S")
    return FieldPhoto(path, match["label"], local.replace(tzinfo=tz))


def exif_orientation(data: bytes) -> int | None:
    return Image.open(BytesIO(data)).getexif().get(0x0112)


def upright_jpeg(data: bytes, extra_rotation: int = 0) -> bytes:
    """JPEG with its pixels upright: EXIF orientation applied, then `extra_rotation`.

    Args:
        data: Original JPEG bytes.
        extra_rotation: Further rotation in degrees, counterclockwise (Pillow's
            convention), for photos that are sideways without an EXIF tag.

    Returns:
        Re-encoded JPEG without an orientation tag (so no viewer rotates it
        again); the other EXIF data is kept.
    """
    image = ImageOps.exif_transpose(Image.open(BytesIO(data)))
    if extra_rotation % 360:
        image = image.rotate(extra_rotation, expand=True)
    exif = image.getexif()
    exif.pop(0x0112, None)
    out = BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY, exif=exif.tobytes())
    return out.getvalue()


def read_rotations(path: Path) -> dict[str, int]:
    """`file name, degrees` lines (counterclockwise); `#` starts a comment."""
    rotations: dict[str, int] = {}
    if not path.exists():
        return rotations
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, degrees = (part.strip() for part in line.split(","))
        rotations[name] = int(degrees)
    return rotations


def contact_sheet(photos: list[tuple[str, bytes]], path: Path, cell: int = 220) -> None:
    """Grid of thumbnails with captions, to check orientations by eye."""
    columns = 7
    rows = -(-len(photos) // columns)
    sheet = Image.new("RGB", (columns * cell, max(rows, 1) * (cell + 18)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, (caption, data) in enumerate(photos):
        thumb = Image.open(BytesIO(data))
        thumb.thumbnail((cell - 6, cell - 6))
        x, y = (i % columns) * cell, (i // columns) * (cell + 18)
        sheet.paste(thumb, (x + 3, y + 3))
        draw.text((x + 3, y + cell), caption, fill="black")
    sheet.save(path, quality=85)
