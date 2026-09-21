from django.core.management.base import BaseCommand, CommandError

from image_index.image_metadata import (
    extract_exif_data_from_bytes,
    parse_datetime_from_exif_dict,
    parse_datetime_from_filename,
)
from image_index.models import Image
from image_index.s3_utils import build_s3_client, get_object_bytes


class Command(BaseCommand):
    help = "Update datetime for existing images using EXIF (or filename fallback)"

    def add_arguments(self, parser):
        parser.add_argument("--image-id", type=int, help="Update only this image ID")
        parser.add_argument(
            "--camera-id", type=int, help="Update only images for this camera"
        )
        parser.add_argument(
            "--force", action="store_true", help="Overwrite existing datetimes"
        )
        parser.add_argument(
            "--filename-only",
            action="store_true",
            help=(
                "Ignore EXIF entirely and derive datetime from the filename only. "
                "Use for cameras whose EXIF clock is known to be wrong."
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Preview changes without saving"
        )
        parser.add_argument("--limit", type=int, help="Max number of images to process")

    def handle(self, *args, **options):
        qs = Image.objects.select_related("camera").order_by("id")

        if options["image_id"]:
            qs = qs.filter(id=options["image_id"])
        if options["camera_id"]:
            qs = qs.filter(camera_id=options["camera_id"])
        if not options["force"]:
            qs = qs.filter(datetime__isnull=True)
        if options["limit"]:
            qs = qs[: options["limit"]]

        if not qs.exists():
            raise CommandError("No images match the given filters")

        filename_only = options["filename_only"]
        s3 = None
        updated = 0
        skipped = 0

        for image in qs:
            exif = None
            update_exif = False

            if filename_only:
                new_dt = parse_datetime_from_filename(image.filename or image.file_name)
            else:
                exif = image.exif_data
                if not exif:
                    if s3 is None:
                        s3 = build_s3_client()
                    try:
                        image_bytes, _ = get_object_bytes(
                            s3, image.bucket, image.object_key
                        )
                        exif = extract_exif_data_from_bytes(image_bytes)
                        update_exif = exif is not None
                    except Exception as exc:
                        self.stderr.write(f"  Cannot fetch {image.object_key}: {exc}")
                        skipped += 1
                        continue

                new_dt = parse_datetime_from_exif_dict(
                    exif
                ) or parse_datetime_from_filename(image.filename or image.file_name)

            if new_dt is None:
                self.stdout.write(f"  No datetime: image={image.id} ({image.filename})")
                skipped += 1
                continue

            if options["dry_run"]:
                self.stdout.write(f"  DRY RUN image={image.id} -> {new_dt}")
                continue

            update_fields = ["datetime"]
            image.datetime = new_dt

            if update_exif:
                image.exif_data = exif
                update_fields.append("exif_data")

            image.save(update_fields=update_fields)
            updated += 1

        self.stdout.write(
            self.style.SUCCESS(f"Done. updated={updated}, skipped={skipped}")
        )
