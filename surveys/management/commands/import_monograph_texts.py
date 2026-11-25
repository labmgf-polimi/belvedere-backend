"""Fill the points' monograph texts from the Publisher monographs.

    manage.py import_monograph_texts data/Monografie_2026_parte1_light.pub \\
        data/Monografie_2026_parte2_light.pub [--apply] [--overwrite]

Reads Target / Descrizione / NOTA of every point page (see
`surveys.utils.publisher`) into the point's `target`, `description` and
`notes`. "Modalità di rilievo GNSS" describes the point across campaigns: its
current part goes to the survey mode (`meas_strategy`) of the point's latest
measurement, and the history ("RTK dal 2023, STATICO in precedenza") to the
point's notes. `.pub` files are converted with LibreOffice first,
so run this where LibreOffice is installed (not in the server image); `.odg`
files from an earlier conversion are read directly.

* Labels match case-insensitively; a page for "D38/D38BIS" fills both points.
* Target and description already set are kept unless `--overwrite`. Notes are
  merged: NOTA and the history are appended if not there yet. The CSV import's
  placeholder note counts as empty, and is cleared on points with a page.
* A measurement's survey mode is only filled when empty — never replaced,
  even with `--overwrite`: an explicit value (e.g. "RTK SPIN3") stays.
* Old notes of the form "Target: ... Descrizione: ..." (the FOTO points) are
  split into the fields.
* "E' un punto FISSO" is only checked against `is_fixed`, never written.

Dry run by default.
"""

import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from surveys.models import Measurement, Point, latest_measurement
from surveys.utils.publisher import (
    TEXT_FIELDS,
    PointPage,
    history_note,
    parse_page,
    publisher_to_odg,
    read_odg_pages,
    split_labelled_notes,
    split_survey_mode,
)

#: Left in `notes` by the measurement CSV import when it created a point.
PLACEHOLDER_NOTES = {"Automatically created during measurement CSV import"}


def current_text(point: Point, field: str) -> str:
    value = (getattr(point, field) or "").strip()
    return "" if field == "notes" and value in PLACEHOLDER_NOTES else value


def merged_notes(old: str, *parts: str) -> str:
    """`old` with each part appended unless it is already in there."""
    for part in parts:
        if part and part not in old:
            old = f"{old} {part}" if old else part
    return old


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
        modes: dict[Measurement, str] = {}
        unmatched, kept, too_long, kept_modes = [], [], [], []
        fixed_conflicts = []
        for page in pages:
            for label in page.labels:
                point = points.get(label.lower())
                if point is None:
                    unmatched.append(label)
                    continue
                if page.fixed and not point.is_fixed:
                    fixed_conflicts.append(point.label)
                latest = latest_measurement(point)
                mode, history = split_survey_mode(
                    page.survey_mode, latest.survey.year if latest else None
                )
                if mode and latest:
                    if (latest.meas_strategy or "").strip():
                        if latest.meas_strategy != mode:
                            kept_modes.append((point.label, latest, mode))
                    else:
                        modes[latest] = mode
                if (point.notes or "").strip() in PLACEHOLDER_NOTES and not page.notes:
                    changes.setdefault(point, {})["notes"] = ""
                texts = {
                    "target": page.target,
                    "description": page.description,
                    "notes": merged_notes(
                        current_text(point, "notes"), page.notes, history_note(history)
                    ),
                }
                for field in TEXT_FIELDS:
                    new, old = texts[field], current_text(point, field)
                    if not new or new == old:
                        continue
                    if lengths[field] and len(new) > lengths[field]:
                        too_long.append(
                            f"{point.label}.{field} ({len(new)} > {lengths[field]})"
                        )
                    elif old and field != "notes" and not options["overwrite"]:
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
        self._report_modes(modes, kept_modes)
        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING("\ndry run: nothing written. Re-run with --apply.")
            )
            return
        with transaction.atomic():
            for point, fields in changes.items():
                Point.objects.filter(pk=point.pk).update(**fields)
            for measurement, mode in modes.items():
                Measurement.objects.filter(pk=measurement.pk).update(meas_strategy=mode)
        self.stdout.write(
            self.style.SUCCESS(
                f"\ndone: {len(changes)} point(s) and {len(modes)} survey mode(s) updated"
            )
        )

    def _report_modes(self, modes, kept_modes) -> None:
        if modes:
            self.stdout.write("\nsurvey mode of the latest measurement:")
            for m, mode in sorted(modes.items(), key=lambda kv: kv[0].point.label):
                self.stdout.write(f"  {m.point.label:<12} {m.survey.year}  {mode}")
        for label, m, mode in kept_modes:
            self.stdout.write(
                f"  {label}: keeps {m.meas_strategy!r} ({m.survey.year}); page says {mode!r}"
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
