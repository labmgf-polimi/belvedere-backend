"""Merge points whose labels differ only by case or surrounding whitespace.

A CSV import that looks points up by exact label creates `D01BIS` alongside an
existing `D01bis`, splitting a stake's history in two. The damage is not just
untidy: each half has its own measurement series, so the orphaned campaign
has no predecessor and no displacement at all in `point_movements`.

Dry-run by default; `--apply` is required to write anything.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import Lower, Trim

from surveys.models import Measurement, Point


def duplicate_groups() -> list[list[Point]]:
    """Points grouped by their normalised label, keeping only real duplicates.

    The canonical point comes first: most measurements, then lowest id. That
    picks the row carrying the history, and falls back to the older row when
    both sides have the same count.
    """
    keys = (
        Point.objects.annotate(key=Lower(Trim("label")))
        .values("key")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("key", flat=True)
    )
    groups = []
    for key in sorted(keys):
        points = list(
            Point.objects.annotate(key=Lower(Trim("label")))
            .filter(key=key)
            .annotate(n=Count("measurement"))
            .order_by("-n", "id")
        )
        groups.append(points)
    return groups


def conflicting_surveys(canonical: Point, duplicate: Point) -> list[int]:
    """Surveys measured on both points — merging would double them up."""
    canonical_surveys = set(
        Measurement.objects.filter(point=canonical).values_list("survey_id", flat=True)
    )
    return sorted(
        survey_id
        for survey_id in Measurement.objects.filter(point=duplicate).values_list(
            "survey_id", flat=True
        )
        if survey_id in canonical_surveys
    )


#: Attributes worth reporting when the two rows disagree. The canonical row
#: always wins; this exists so a silent difference gets seen.
COMPARED_FIELDS = ("active", "is_fixed", "ref_date")


def differing_fields(canonical: Point, duplicate: Point) -> list[str]:
    return [
        field
        for field in COMPARED_FIELDS
        if getattr(canonical, field) != getattr(duplicate, field)
    ]


class Command(BaseCommand):
    help = "Merge points whose labels differ only by case or whitespace."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform the merge. Without it the command only reports.",
        )

    def handle(self, *args, **options) -> None:
        groups = duplicate_groups()
        if not groups:
            self.stdout.write(self.style.SUCCESS("no duplicate labels"))
            return

        blocked: list[str] = []
        planned: list[tuple[Point, list[Point], int]] = []

        for points in groups:
            canonical, duplicates = points[0], points[1:]
            moving = 0
            for duplicate in duplicates:
                collisions = conflicting_surveys(canonical, duplicate)
                if collisions:
                    blocked.append(
                        f"{duplicate.label} (id {duplicate.pk}) -> "
                        f"{canonical.label} (id {canonical.pk}): "
                        f"both measure survey(s) {collisions}"
                    )
                    continue
                moving += duplicate.n
            planned.append((canonical, duplicates, moving))

        for canonical, duplicates, moving in planned:
            self.stdout.write(
                f"\n  {canonical.label} (id {canonical.pk}, {canonical.n} measurements)"
                f"  <- keeps"
            )
            for duplicate in duplicates:
                differences = differing_fields(canonical, duplicate)
                note = f"  differs on {', '.join(differences)}" if differences else ""
                self.stdout.write(
                    f"    {duplicate.label} (id {duplicate.pk}, "
                    f"{duplicate.n} measurements){note}"
                )
            self.stdout.write(f"    -> {moving} measurement(s) re-pointed")

        if blocked:
            for line in blocked:
                self.stdout.write(self.style.ERROR(f"\n  BLOCKED  {line}"))
            raise CommandError(
                f"{len(blocked)} group(s) would collide; resolve them by hand first"
            )

        if not options["apply"]:
            total = sum(moving for _, _, moving in planned)
            self.stdout.write(
                self.style.WARNING(
                    f"\ndry run: {len(planned)} group(s), {total} measurement(s) "
                    f"would move. Re-run with --apply to perform the merge."
                )
            )
            return

        with transaction.atomic():
            moved = removed = 0
            for canonical, duplicates, _ in planned:
                for duplicate in duplicates:
                    moved += Measurement.objects.filter(point=duplicate).update(
                        point=canonical
                    )
                    duplicate.delete()
                    removed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nmerged: {moved} measurement(s) re-pointed, "
                f"{removed} duplicate point(s) deleted"
            )
        )
        self.stdout.write(
            "Displacements recompute automatically — point_movements is a view."
        )
