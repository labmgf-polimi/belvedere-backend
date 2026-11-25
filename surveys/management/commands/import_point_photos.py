"""Attach a campaign's field photos to its measurements and set their time.

    manage.py import_point_photos data/point_photos --year 2026 [--apply]

Each `Img_<LABEL>_<DDMMYY>_<HHMMSS>.jpg` (see `surveys.utils.photos`):

* goes to the measurement of point <LABEL> (case-insensitive; `--alias` for
  misspelt names) in survey year `--year`;
* is stored upright: EXIF orientation applied, plus the extra counterclockwise
  rotation listed for it in `rotations.csv` (`file name, degrees`) — untagged
  photos can be sideways and only a human can tell;
* gives the measurement its real time: `meas_time` (stored in UTC) and
  `meas_date` (local date) from the earliest photo, read in `--tz`. Existing
  values are kept unless `--overwrite-times`; the survey date is never touched.

Photos already attached (same file name) are skipped, so re-running is safe;
`--replace-attached` re-uploads them upright instead (e.g. originals uploaded
through the admin before this command existed).

Dry run by default: prints every match and change, the effect on the yearly
velocities, and writes a contact sheet of the final orientations to check by
eye.
"""

from collections import defaultdict
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from surveys.models import Measurement, MeasurementPhoto, PointMovement
from surveys.utils.photos import (
    FIELD_TIME_ZONE,
    FieldPhoto,
    contact_sheet,
    exif_orientation,
    parse_photo_name,
    read_rotations,
    upright_jpeg,
)

#: Warn when a photo's date is further than this from its measurement's date.
MAX_DATE_GAP_DAYS = 3


class DryRun(Exception):
    """Raised to roll back the dry-run's trial update."""


def measurement_day(measurement: Measurement) -> date:
    return measurement.meas_date or measurement.survey.date


class Command(BaseCommand):
    help = "Attach field photos to a campaign's measurements and set measurement times."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "directory", type=Path, help="Folder with Img_*.jpg photos."
        )
        parser.add_argument("--year", type=int, required=True, help="Survey year.")
        parser.add_argument(
            "--tz",
            default=str(FIELD_TIME_ZONE),
            help="Time zone of the photo names (the controller clock). Default: %(default)s.",
        )
        parser.add_argument(
            "--alias",
            action="append",
            default=[],
            metavar="NAME=LABEL",
            help="Point label for a misspelt photo label, e.g. DS02TER=D02ter. Repeatable.",
        )
        parser.add_argument(
            "--rotations",
            type=Path,
            help="Extra rotations file (default: <directory>/rotations.csv).",
        )
        parser.add_argument(
            "--contact-sheet",
            type=Path,
            help="Where to write the review sheet (default: <directory>/_contact_sheet.jpg).",
        )
        parser.add_argument(
            "--overwrite-times",
            action="store_true",
            help="Replace measurement dates/times that are already set.",
        )
        parser.add_argument(
            "--replace-attached",
            action="store_true",
            help="Re-upload photos already attached (same file name) in upright form.",
        )
        parser.add_argument("--apply", action="store_true", help="Write the changes.")

    def handle(self, *args, **options) -> None:
        directory: Path = options["directory"]
        if not directory.is_dir():
            raise CommandError(f"not a directory: {directory}")
        tz = ZoneInfo(options["tz"])
        aliases = self._aliases(options["alias"])
        rotations = read_rotations(options["rotations"] or directory / "rotations.csv")

        photos, unparsed = [], []
        for path in sorted(directory.iterdir()):
            if path.is_dir() or path.name.startswith("_") or path.suffix == ".csv":
                continue
            photo = parse_photo_name(path, tz)
            (photos if photo else unparsed).append(photo or path.name)

        by_measurement, unmatched = self._match(photos, options["year"], aliases)
        self._report(by_measurement, unmatched, unparsed, rotations, options)

        sheet = options["contact_sheet"] or directory / "_contact_sheet.jpg"
        uploads = self._prepare(
            by_measurement, rotations, sheet, options["replace_attached"]
        )
        times = self._new_times(by_measurement, options["overwrite_times"])
        self._velocity_effect(times)

        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\ndry run: nothing written. Check {sheet}, then re-run with --apply."
                )
            )
            return
        self._apply(uploads, times)

    # --- steps -----------------------------------------------------------

    def _aliases(self, pairs: list[str]) -> dict[str, str]:
        aliases = {}
        for pair in pairs:
            name, _, label = pair.partition("=")
            if not label:
                raise CommandError(f"--alias needs NAME=LABEL, got {pair!r}")
            aliases[name.strip().lower()] = label.strip()
        return aliases

    def _match(
        self, photos: list[FieldPhoto], year: int, aliases: dict[str, str]
    ) -> tuple[dict[Measurement, list[FieldPhoto]], list[FieldPhoto]]:
        candidates: dict[str, list[Measurement]] = defaultdict(list)
        for m in Measurement.objects.filter(survey__year=year).select_related(
            "point", "survey"
        ):
            candidates[m.point.label.lower()].append(m)
        if not candidates:
            raise CommandError(f"no measurements in survey year {year}")

        by_measurement: dict[Measurement, list[FieldPhoto]] = defaultdict(list)
        unmatched = []
        for photo in photos:
            label = aliases.get(photo.label.lower(), photo.label).lower()
            options = candidates.get(label)
            if not options:
                unmatched.append(photo)
                continue
            day = photo.taken_at.date()
            # Several campaigns in one year: the measurement closest in time.
            best = min(options, key=lambda m: abs((measurement_day(m) - day).days))
            by_measurement[best].append(photo)
        self.year_measurements = [m for ms in candidates.values() for m in ms]
        return by_measurement, unmatched

    def _report(self, by_measurement, unmatched, unparsed, rotations, options) -> None:
        self.stdout.write(f"\n{'point':<12} {'photo(s)':<44} measurement time (local)")
        for m, photos in sorted(
            by_measurement.items(), key=lambda kv: kv[0].point.label
        ):
            names = ", ".join(
                p.name + (f" +{rotations[p.name]}°" if rotations.get(p.name) else "")
                for p in photos
            )
            first = min(p.taken_at for p in photos)
            gap = abs((measurement_day(m) - first.date()).days)
            warn = (
                f"  !! {gap} days from {measurement_day(m)}"
                if gap > MAX_DATE_GAP_DAYS
                else ""
            )
            self.stdout.write(
                f"{m.point.label:<12} {names:<44} {first:%Y-%m-%d %H:%M:%S %Z}{warn}"
            )

        missing = sorted(
            m.point.label for m in self.year_measurements if m not in by_measurement
        )
        if missing:
            self.stdout.write(
                f"\nmeasurements of {options['year']} without a photo: {', '.join(missing)}"
            )
        for photo in unmatched:
            self.stdout.write(
                self.style.ERROR(
                    f"no measurement of {options['year']} for {photo.name} "
                    f"(label {photo.label!r}; use --alias {photo.label}=<label>)"
                )
            )
        for name in unparsed:
            self.stdout.write(
                self.style.WARNING(
                    f"skipped, not an Img_<LABEL>_<DDMMYY>_<HHMMSS>.jpg name: {name}"
                )
            )
        unknown = set(rotations) - {
            p.name for ps in by_measurement.values() for p in ps
        }
        for name in sorted(unknown):
            self.stdout.write(
                self.style.WARNING(f"rotations.csv lists an unknown file: {name}")
            )

    def _prepare(
        self, by_measurement, rotations, sheet: Path, replace: bool
    ) -> list[tuple[Measurement, str, bytes, MeasurementPhoto | None]]:
        """Upright JPEGs to upload, with the photo each replaces; writes the sheet."""
        uploads, previews = [], []
        for m, photos in sorted(
            by_measurement.items(), key=lambda kv: kv[0].point.label
        ):
            attached = {p.file_name: p for p in m.photos.all()}
            for photo in photos:
                original = photo.path.read_bytes()
                upright = upright_jpeg(original, rotations.get(photo.name, 0))
                tag = exif_orientation(original)
                existing = attached.get(photo.name)
                status = (
                    "new" if not existing else ("replace" if replace else "attached")
                )
                previews.append(
                    (
                        f"{m.point.label} {photo.taken_at:%H:%M} exif={tag or '-'} "
                        f"rot={rotations.get(photo.name, 0)} {status}",
                        upright,
                    )
                )
                if not existing or replace:
                    uploads.append((m, photo.name, upright, existing))
        contact_sheet(previews, sheet)
        replacing = sum(1 for *_, existing in uploads if existing)
        skipped = len(previews) - len(uploads)
        self.stdout.write(
            f"\nphotos: {len(uploads) - replacing} to upload, {replacing} to replace "
            f"with the upright version, {skipped} already attached (kept); "
            f"contact sheet: {sheet}"
        )
        return uploads

    def _new_times(self, by_measurement, overwrite: bool) -> dict[Measurement, dict]:
        times = {}
        kept = 0
        for m, photos in by_measurement.items():
            taken = min(p.taken_at for p in photos)
            new = {}
            if m.meas_time is None or overwrite:
                new["meas_time"] = taken
            if m.meas_date is None or overwrite:
                new["meas_date"] = taken.date()
            elif m.meas_date != taken.date():
                self.stdout.write(
                    self.style.WARNING(
                        f"{m.point.label}: meas_date {m.meas_date} kept, photo says {taken.date()}"
                    )
                )
            if new:
                times[m] = new
            else:
                kept += 1
        self.stdout.write(
            f"measurement times: {len(times)} to set"
            + (
                f", {kept} already set (kept; --overwrite-times replaces them)"
                if kept
                else ""
            )
        )
        return times

    def _velocity_effect(self, times: dict[Measurement, dict]) -> None:
        """Trial-apply the new dates and compare the yearly velocities, then roll back."""
        labels = {m.point.label for m in times}
        if not labels:
            return
        annual = PointMovement.objects.filter(label__in=labels, is_annual=True)
        before = {r.id: (r.dt, r.v) for r in annual}
        try:
            with transaction.atomic():
                for m, fields in times.items():
                    Measurement.objects.filter(pk=m.pk).update(**fields)
                after = {r.id: (r.dt, r.v) for r in annual.all()}
                raise DryRun
        except DryRun:
            pass
        changed = [
            (rid, before[rid], after[rid])
            for rid in before.keys() & after.keys()
            if before[rid] != after[rid]
        ]
        if not changed:
            self.stdout.write("yearly velocities: unchanged")
            return
        rel = [abs(a[1] - b[1]) / b[1] for _, b, a in changed if b[1]]
        ddt = sorted({a[0] - b[0] for _, b, a in changed})
        self.stdout.write(
            f"yearly velocities: {len(changed)} movement(s) change; "
            f"interval length by {ddt} days; v by at most {max(rel):.2%} "
            f"(mean {sum(rel) / len(rel):.2%})"
        )

    def _apply(self, uploads, times) -> None:
        for m, fields in times.items():
            Measurement.objects.filter(pk=m.pk).update(**fields)
        for m, name, data, existing in uploads:
            # One transaction per photo: a failure keeps the earlier uploads.
            with transaction.atomic():
                if existing:
                    # The model deletes the old object (only within this
                    # environment's S3 prefix) once the new one is saved.
                    existing.image = ContentFile(data, name=name)
                    existing.save()
                else:
                    MeasurementPhoto.objects.create(
                        measurement=m, image=ContentFile(data, name=name)
                    )
            self.stdout.write(
                f"  {'replaced' if existing else 'uploaded'} {name} -> {m.point.label}"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"\ndone: {len(uploads)} photo(s) uploaded or replaced, "
                f"{len(times)} measurement time(s) set"
            )
        )
