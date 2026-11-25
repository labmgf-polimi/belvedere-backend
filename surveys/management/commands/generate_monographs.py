"""Point monographs as PDF, from the database only.

    manage.py generate_monographs                      # active points, latest data
    manage.py generate_monographs --until-year 2025    # as of the 2025 survey
    manage.py generate_monographs --points D12 D38bis  # selected points
    manage.py generate_monographs --split              # also one PDF per point

One combined PDF (cover with an index, then one page per point) is written
to `--output`, named `monographs_<year|latest>_<date>.pdf`; `--split` adds
`<output>/points/<label>.pdf`. Each page uses the point's latest measurement up
to `--until-year` and its 1–2 most recent photos up to that year (empty boxes
otherwise), so re-run it after uploading photos. The overview imagery (Esri
World Imagery) is downloaded once and cached in `--tile-cache`.
"""

import re
import tempfile
import time
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from surveys.monographs import prepare, render_html, select_points, write_pdf


class Command(BaseCommand):
    help = "Generate the point monographs (PDF)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--until-year", type=int, help="Use data up to this survey year."
        )
        parser.add_argument(
            "--points", nargs="+", metavar="LABEL", help="Only these points."
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="All points, not only active ones.",
        )
        parser.add_argument(
            "--split", action="store_true", help="Also one PDF per point."
        )
        parser.add_argument(
            "--output",
            type=Path,
            default=Path("monographs"),
            help="Output folder (default: %(default)s).",
        )
        parser.add_argument(
            "--tile-cache",
            type=Path,
            default=settings.MONOGRAPH_TILE_CACHE,
            help="Satellite tile cache (default: %(default)s).",
        )

    def handle(self, *args, **options) -> None:
        started = time.monotonic()
        year = options["until_year"]
        points = select_points(options["points"], options["include_inactive"])
        if not points:
            raise CommandError("no points selected")

        with tempfile.TemporaryDirectory() as tmp:
            self.stdout.write(f"{len(points)} point(s); collecting data and imagery...")
            mset = prepare(
                points, year, Path(tmp), options["tile_cache"], photo_files=True
            )
            if mset.skipped:
                until = f" up to {year}" if year else ""
                self.stdout.write(
                    self.style.WARNING(
                        f"no measurement{until}, skipped: {', '.join(mset.skipped)}"
                    )
                )
            if not mset.monographs:
                raise CommandError("nothing to generate")

            out = (
                options["output"]
                / f"monographs_{year or 'latest'}_{date.today():%Y%m%d}.pdf"
            )
            _, pages = write_pdf(render_html(mset, year), out)
            self.stdout.write(self.style.SUCCESS(f"{out}  ({pages} pages)"))

            if options["split"]:
                for m in mset.monographs:
                    name = re.sub(r"\W+", "_", m.point.label) + ".pdf"
                    html = render_html(mset, year, cover=False, monographs=[m])
                    write_pdf(html, options["output"] / "points" / name)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{options['output'] / 'points'}/  ({len(mset.monographs)} files)"
                    )
                )

        no_photo = sum(1 for m in mset.monographs if not m.photos)
        self.stdout.write(
            f"done in {time.monotonic() - started:.0f} s; "
            f"{no_photo} point(s) without photos (empty boxes)"
        )
