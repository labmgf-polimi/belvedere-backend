"""Point texts from the Publisher monographs (Monografie_*.pub).

LibreOffice converts a `.pub` to OpenDocument Drawing (`.odg`); its
`content.xml` holds one `draw:page` per point. A page reads:

    D38/D38BIS                            <- label(s)
    Politecnico di Milano ...             <- header, ignored
    Target: giallo e nero
    Descrizione: Lingua del ghiacciaio ...
    E' un punto FISSO                     <- point type, not a description
    Modalità di rilievo GNSS: RTK RADIO, nRTK (2024)
    NOTA: Target sostituito nel 2026 ...
    Coordinate 27-07-2024                 <- coordinate tables, ignored

Labels can also run on inside a paragraph ("...nel 2026Descrizione: ..."), and
"E' un punto FISSO" can close a description. Other unlabelled paragraphs after
the first field are kept as notes.
"""

import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"

#: Page label (lower case) -> Point field.
FIELDS = {
    "target": "target",
    "descrizione": "description",
    "modalità di rilievo gnss": "survey_mode",
    "nota": "notes",
}
TEXT_FIELDS = ("target", "description", "survey_mode", "notes")
FIXED = re.compile(r"^e[’']\s*un punto fisso", re.IGNORECASE)
#: Where a label or the fixed-point remark starts, even mid-paragraph.
SEGMENT_START = re.compile(
    r"(?=Target\s*:|Descrizione\s*:|Modalità di rilievo GNSS\s*:|NOTA\s*:|E[’']\s*un punto FISSO)"
)


@dataclass
class PointPage:
    labels: list[str]
    target: str = ""
    description: str = ""
    survey_mode: str = ""
    notes: str = ""
    #: The page says "E' un punto FISSO".
    fixed: bool = False

    def add(self, field: str, text: str) -> None:
        if text:
            current = getattr(self, field)
            setattr(self, field, f"{current} {text}" if current else text)


def normalise(text: str) -> str:
    """One line, single spaces, composed accents.

    The pages mix in non-breaking spaces and decomposed letters ("a" + U+0300
    for "à"), which would make the same label look different.
    """
    text = unicodedata.normalize("NFC", text).replace("\xa0", " ")
    return " ".join(text.split())


def parse_page(paragraphs: list[str]) -> PointPage | None:
    """The texts of one point page; None for pages that are not a point (cover...)."""
    lines = [line for line in map(normalise, paragraphs) if line]
    if len(lines) < 2 or not any(line.lower().startswith("target") for line in lines):
        return None
    page = PointPage([label.strip() for label in lines[0].split("/") if label.strip()])
    current = None
    for line in lines[1:]:
        if line.startswith("Coordinate"):
            break
        for i, segment in enumerate(s.strip() for s in SEGMENT_START.split(line)):
            if not segment:
                continue
            key, colon, value = segment.partition(":")
            field = FIELDS.get(key.strip().lower()) if colon else None
            if field:
                current = field
                page.add(field, value.strip())
            elif FIXED.match(segment):
                page.fixed = True
                # "E' un punto FISSO instabile (...)": the rest is worth keeping.
                if FIXED.sub("", segment).strip(" .:"):
                    page.add("notes", segment)
            elif current is None:  # before the first field: the header
                continue
            elif i == 0:  # an unlabelled paragraph of its own
                page.add("notes", segment)
            else:  # text after an inline label or remark: same field
                page.add(current, segment)
    return page


def read_odg_pages(path: Path) -> list[list[str]]:
    """Paragraph texts of each page of an `.odg` file."""
    with zipfile.ZipFile(path) as odg:
        root = ET.fromstring(odg.read("content.xml"))
    return [
        ["".join(p.itertext()) for p in page.iter(f"{{{TEXT}}}p")]
        for page in root.iter(f"{{{DRAW}}}page")
    ]


def publisher_to_odg(path: Path, out_dir: Path) -> Path:
    """Convert a `.pub` with LibreOffice (needs `soffice`; takes about a minute)."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        raise RuntimeError("LibreOffice (soffice) is needed to read .pub files")
    with tempfile.TemporaryDirectory() as profile:
        # A private profile, so a running LibreOffice does not block the conversion.
        subprocess.run(
            [soffice, f"-env:UserInstallation=file://{profile}", "--headless",
             "--convert-to", "odg", "--outdir", str(out_dir), str(path)],
            check=True, capture_output=True, timeout=900,
        )  # fmt: skip
    odg = out_dir / f"{path.stem}.odg"
    if not odg.exists():
        raise RuntimeError(f"LibreOffice did not convert {path}")
    return odg


def split_labelled_notes(notes: str) -> dict[str, str]:
    """Fields out of old notes written as 'Target: ... Descrizione: ...'.

    The FOTO points kept their monograph texts in `notes`; only notes that
    start with a label are split, and the leftover text stays in notes.
    """
    text = normalise(notes)
    if not re.match(r"(?i)^target\s*:", text):
        return {}
    parts = re.split(r"(?i)\b(target|descrizione)\s*:", text)
    found = {"notes": parts[0].strip()}
    for key, value in zip(parts[1::2], parts[2::2], strict=True):
        found[FIELDS[key.lower()]] = value.strip()
    return found
