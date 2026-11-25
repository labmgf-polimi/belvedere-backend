"""Fill the points' monograph texts from the Publisher monographs.

    manage.py import_monograph_texts data/Monografie_2026_parte1_light.pub \\
        data/Monografie_2026_parte2_light.pub [--apply] [--overwrite]

Reads Target / Descrizione / Modalità di rilievo GNSS / NOTA of every point
page (see `surveys.utils.publisher`) into `target`, `description`,
`survey_mode` and `notes`. `.pub` files are converted with LibreOffice first,
so run this where LibreOffice is installed (not in the server image); `.odg`
files from an earlier conversion are read directly.

* Labels match case-insensitively; a page for "D38/D38BIS" fills both points.
* A field that already has text is kept unless `--overwrite`. The CSV import's
  placeholder note counts as empty, and is cleared on points with a page.
* Old notes of the form "Target: ... Descrizione: ..." (the FOTO points) are
  split into the fields.
* "E' un punto FISSO" is only checked against `is_fixed`, never written.

Dry run by default.
"""

import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from surveys.models import Point
from surveys.utils.publisher import (
    TEXT_FIELDS,
    PointPage,
    parse_page,
    publisher_to_odg,
    read_odg_pages,
    split_labelled_notes,
)

#: Left in `notes` by the measurement CSV import when it created a point.
PLACEHOLDER_NOTES = {"Automatically created during measurement CSV import"}


def current_text(point: Point, field: str) -> str:
    value = (getattr(point, field) or "").strip()
    return "" if field == "notes" and value in PLACEHOLDER_NOTES else value


class Command(BaseCommand):
    help = "Fill the points' monograph texts from the Publisher monographs."

    def add_arguments(self, parser) -> None:
        parser.add_argument("files", nargs="+", type=Path, help=".pub or .odg files")
        parser.add_argument(
            "--overwrite", action="store_true", help="Replace existing texts."
        )
        parser.add_argument("--apply", action="store_true", help="Write the changes.")

    def handle(self, *args, **options) -> None:
        pages = self._read_pages(options["files"])
        points = {p.label.lower(): p for p in Point.objects.exclude(label=None)}
        lengths = {f: Point._meta.get_field(f).max_length for f in TEXT_FIELDS}

        changes: dict[Point, dict[str, str]] = {}
        unmatched, kept, too_long = [], [], []
        fixed_conflicts = []
        for page in pages:
            for label in page.labels:
                point = points.get(label.lower())
                if point is None:
                    unmatched.append(label)
                    continue
                if page.fixed and not point.is_fixed:
                    fixed_conflicts.append(point.label)
                if (point.notes or "").strip() in PLACEHOLDER_NOTES and not page.notes:
                    changes.setdefault(point, {})["notes"] = ""
                for field in TEXT_FIELDS:
                    new, old = getattr(page, field), current_text(point, field)
                    if not new or new == old:
                        continue
                    if lengths[field] and len(new) > lengths[field]:
                        too_long.append(
                            f"{point.label}.{field} ({len(new)} > {lengths[field]})"
                        )
                    elif old and not options["overwrite"]:
                        kept.append((point.label, field, old, new))
                    else:
                        changes.setdefault(point, {})[field] = new

        paged = {label.lower() for page in pages for label in page.labels}
        for point in points.values():
            if point.label.lower() in paged:
                continue
            split = split_labelled_notes(point.notes or "")
            fields = {
                f: v for f, v in split.items()
                if f == "notes" or not current_text(point, f) or options["overwrite"]
            }  # fmt: skip
            if any(f != "notes" for f in fields):
                changes[point] = fields

        self._report(changes, unmatched, kept, too_long, fixed_conflicts)
        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING("\ndry run: nothing written. Re-run with --apply.")
            )
            return
        with transaction.atomic():
            for point, fields in changes.items():
                Point.objects.filter(pk=point.pk).update(**fields)
        self.stdout.write(
            self.style.SUCCESS(f"\ndone: {len(changes)} point(s) updated")
        )

    def _read_pages(self, files: list[Path]) -> list[PointPage]:
        pages = []
        with tempfile.TemporaryDirectory() as tmp:
            for path in files:
                if not path.exists():
                    raise CommandError(f"no such file: {path}")
                if path.suffix.lower() == ".pub":
                    self.stdout.write(f"converting {path.name} with LibreOffice...")
                    try:
                        path = publisher_to_odg(path, Path(tmp))
                    except (RuntimeError, OSError) as exc:
                        raise CommandError(str(exc)) from exc
                found = [p for p in map(parse_page, read_odg_pages(path)) if p]
                self.stdout.write(f"{path.name}: {len(found)} point page(s)")
                pages += found
        return pages

    def _report(self, changes, unmatched, kept, too_long, fixed_conflicts) -> None:
        for point, fields in sorted(changes.items(), key=lambda kv: kv[0].label):
            self.stdout.write(f"\n{point.label}")
            for field, value in fields.items():
                self.stdout.write(f"  {field:<12} {value or '(cleared)'}")
        if kept:
            self.stdout.write(
                self.style.WARNING("\nkept existing text (--overwrite replaces it):")
            )
            for label, field, old, new in kept:
                self.stdout.write(
                    f"  {label}.{field}: keeps {old!r}\n      page says {new!r}"
                )
        for item in too_long:
            self.stdout.write(self.style.ERROR(f"too long, skipped: {item}"))
        if unmatched:
            self.stdout.write(
                self.style.WARNING(
                    f"\npages with no such point: {', '.join(unmatched)}"
                )
            )
        if fixed_conflicts:
            self.stdout.write(
                self.style.WARNING(
                    "\npage says 'punto FISSO' but is_fixed is false (not changed): "
                    + ", ".join(fixed_conflicts)
                )
            )
        self.stdout.write(f"\n{len(changes)} point(s) to update")
