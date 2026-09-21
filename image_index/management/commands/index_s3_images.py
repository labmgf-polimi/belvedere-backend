import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max

from image_index.image_metadata import (
    IMAGE_EXTENSIONS,
    extract_image_metadata_from_bytes,
    parse_datetime_from_filename,
)
from image_index.models import Camera, Image
from image_index.s3_utils import build_s3_client, get_object_bytes

INCREMENTAL_BUFFER_DAYS = 7

BATCH_SIZE = 200
DEFAULT_WORKERS = 2  # Keep low to run on vps

Logger = Callable[[str], None]

_UPDATE_FIELDS = [
    "camera",
    "filename",
    "file_size_bytes",
    "s3_etag",
    "s3_last_modified",
    "datetime",
    "width_px",
    "height_px",
    "mime_type",
    "exif_data",
    "rotation",
    "is_indexed",
    "updated_at",
]


@dataclass
class IndexCounts:
    seen: int = 0
    matched: int = 0
    filtered: int = 0
    skipped: int = 0
    upserted: int = 0

    def __iadd__(self, other: "IndexCounts") -> "IndexCounts":
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))
        return self

    def summary(self) -> str:
        return " ".join(f"{f.name}={getattr(self, f.name)}" for f in fields(self))


@dataclass
class FetchTask:
    key: str
    filename: str
    etag: str | None
    size: int | None
    last_modified: datetime | None


def _fetch_and_extract(s3, bucket: str, key: str, filename: str) -> dict:
    """Fetch image bytes from S3 and extract metadata. Designed to run in a thread."""
    try:
        image_bytes, mime_type = get_object_bytes(s3, bucket, key)
        return extract_image_metadata_from_bytes(
            image_bytes,
            filename=filename,
            mime_type=mime_type,
        )
    except Exception:
        return {
            "datetime": parse_datetime_from_filename(filename),
            "width_px": None,
            "height_px": None,
            "mime_type": None,
            "exif_data": None,
            "rotation": 0,
        }


def _resolve_bucket_and_prefix(
    camera: Camera, bucket_override: str | None, prefix_override: str | None
) -> tuple[str, str]:
    bucket = bucket_override or camera.s3_bucket
    prefix = prefix_override if prefix_override is not None else camera.s3_prefix
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"
    return bucket, prefix or ""


def _compute_buffer_start(
    camera: Camera, bucket: str, incremental: bool, force: bool, buffer_days: int
) -> tuple[datetime | None, str | None]:
    """Return (buffer_start, log message) for incremental mode, or (None, None) if disabled."""
    if not incremental or force:
        return None, None

    watermark = Image.objects.filter(camera=camera, bucket=bucket).aggregate(
        Max("s3_last_modified")
    )["s3_last_modified__max"]
    if watermark is None:
        return None, "incremental mode: no watermark yet, full scan"

    buffer_start = watermark - timedelta(days=buffer_days)
    return buffer_start, (
        f"incremental mode: watermark={watermark.isoformat()} "
        f"buffer_start={buffer_start.isoformat()}"
    )


def _load_existing_etags(
    camera: Camera, bucket: str, buffer_start: datetime | None
) -> dict[str, str | None]:
    qs = Image.objects.filter(camera=camera, bucket=bucket)
    if buffer_start is not None:
        qs = qs.filter(s3_last_modified__gte=buffer_start)
    return dict(qs.values_list("object_key", "s3_etag"))


def _select_objects_to_fetch(
    contents: list[dict],
    key_filter_re: re.Pattern | None,
    existing_etags: dict[str, str | None],
    buffer_start: datetime | None,
    force: bool,
    limit: int | None,
    counts: IndexCounts,
) -> tuple[list[FetchTask], bool]:
    """Classify one S3 listing page into fetch tasks, updating counts in place.

    Returns (tasks, limit_reached).
    """
    tasks: list[FetchTask] = []
    for obj in contents:
        key = obj["Key"]
        if key.endswith("/") or not key.lower().endswith(IMAGE_EXTENSIONS):
            continue

        if key_filter_re is not None and not key_filter_re.search(key):
            counts.filtered += 1
            continue

        counts.matched += 1

        etag = (obj.get("ETag") or "").strip('"') or None
        size = obj.get("Size")
        last_modified = obj.get("LastModified")
        filename = PurePosixPath(key).name

        # Incremental: skip objects older than the buffer window — they are
        # guaranteed to be indexed already.
        if (
            buffer_start is not None
            and last_modified is not None
            and last_modified < buffer_start
        ):
            counts.skipped += 1
            continue

        # Skip if ETag matches an existing row (content unchanged).
        if not force and key in existing_etags and existing_etags[key] == etag:
            counts.skipped += 1
            continue

        tasks.append(FetchTask(key, filename, etag, size, last_modified))

        if limit and counts.matched >= limit:
            return tasks, True

    return tasks, False


def _build_image(camera: Camera, bucket: str, task: FetchTask, meta: dict) -> Image:
    return Image(
        camera=camera,
        bucket=bucket,
        object_key=task.key,
        filename=task.filename,
        file_size_bytes=task.size,
        s3_etag=task.etag,
        s3_last_modified=task.last_modified,
        datetime=meta["datetime"],
        width_px=meta["width_px"],
        height_px=meta["height_px"],
        mime_type=meta["mime_type"],
        exif_data=meta["exif_data"],
        rotation=meta["rotation"],
        is_indexed=True,
    )


def _flush_batch(
    batch: list[Image],
    batch_size: int,
    counts: IndexCounts,
    log: Logger,
) -> None:
    if not batch:
        return

    n = len(batch)
    Image.objects.bulk_create(
        batch,
        batch_size=batch_size,
        update_conflicts=True,
        update_fields=_UPDATE_FIELDS,
        unique_fields=["bucket", "object_key"],
    )
    counts.upserted += n
    log(f"  flushed {n} rows | {counts.summary()}")
    batch.clear()


def _index_camera(
    s3, camera: Camera, options: dict, log: Logger, err: Logger
) -> IndexCounts:
    """Index a single camera's S3 prefix into the Image table."""
    counts = IndexCounts()
    bucket, prefix = _resolve_bucket_and_prefix(
        camera, options["bucket"], options["prefix"]
    )
    key_filter_re = (
        re.compile(camera.s3_key_filter_regex) if camera.s3_key_filter_regex else None
    )
    workers = max(1, options["workers"])
    batch_size = options["batch_size"]
    dry_run = options["dry_run"]
    limit = options["limit"]

    log(
        f"\nIndexing camera={camera.id} name={camera.camera_name} "
        f"bucket={bucket} prefix={prefix} "
        f"key_filter={camera.s3_key_filter_regex or '(none)'} workers={workers}"
    )

    buffer_start, buffer_message = _compute_buffer_start(
        camera,
        bucket,
        options["incremental"],
        options["force"],
        options["incremental_buffer_days"],
    )
    if buffer_message:
        log(f"  {buffer_message}")

    existing_etags = _load_existing_etags(camera, bucket, buffer_start)
    log(f"  {len(existing_etags)} rows loaded from DB")

    paginator = s3.get_paginator("list_objects_v2")
    page_iter = paginator.paginate(
        Bucket=bucket,
        Prefix=prefix,
        PaginationConfig={"PageSize": 1000},
    )

    batch: list[Image] = []
    done = False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for page_num, page in enumerate(page_iter, start=1):
            if done:
                break

            contents = page.get("Contents", [])
            counts.seen += len(contents)

            to_fetch, done = _select_objects_to_fetch(
                contents,
                key_filter_re,
                existing_etags,
                buffer_start,
                options["force"],
                limit,
                counts,
            )

            log(
                f"  page {page_num}: {len(contents)} objects | "
                f"to fetch={len(to_fetch)} | skipped so far={counts.skipped}"
            )

            if not to_fetch:
                continue

            futures = {
                pool.submit(
                    _fetch_and_extract, s3, bucket, task.key, task.filename
                ): task
                for task in to_fetch
            }

            for future in as_completed(futures):
                task = futures[future]
                try:
                    meta = future.result()
                except Exception as exc:
                    err(f"  ERROR {task.key}: {exc}")
                    continue

                if dry_run:
                    log(
                        f"  DRY RUN {task.key} | dt={meta['datetime']} | "
                        f"size={meta['width_px']}x{meta['height_px']}"
                    )
                    continue

                batch.append(_build_image(camera, bucket, task, meta))
                if len(batch) >= batch_size:
                    _flush_batch(batch, batch_size, counts, log)

            if done:
                break

    _flush_batch(batch, batch_size, counts, log)
    return counts


class Command(BaseCommand):
    help = "Index images from S3 buckets into the Image table"

    def add_arguments(self, parser):
        parser.add_argument("--camera-id", type=int, help="Index only one camera by ID")
        parser.add_argument("--bucket", type=str, help="Override bucket name")
        parser.add_argument(
            "--prefix", type=str, help="Override prefix for selected camera"
        )
        parser.add_argument(
            "--incremental",
            action="store_true",
            help=(
                "Skip objects older than the watermark (max s3_last_modified in DB minus --incremental-buffer-days). Faster for routine runs when most objects are already indexed."
            ),
        )
        parser.add_argument(
            "--incremental-buffer-days",
            type=int,
            default=INCREMENTAL_BUFFER_DAYS,
            help=(
                "How many days before the watermark to still re-check (default: 7). Covers re-uploads and clock skew."
            ),
        )
        parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
        parser.add_argument(
            "--workers",
            type=int,
            default=DEFAULT_WORKERS,
            help="Number of parallel threads for S3 fetch + EXIF extraction (default: 8)",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-fetch and re-index even unchanged objects (ETag match)",
        )
        parser.add_argument(
            "--limit", type=int, help="Limit number of objects to process (for testing)"
        )

    def handle(self, *args, **options):
        if not settings.S3_ENDPOINT_URL:
            raise CommandError("S3_ENDPOINT_URL is not configured")
        if not settings.S3_ACCESS_KEY or not settings.S3_SECRET_KEY:
            raise CommandError("S3 credentials are not configured")

        s3 = build_s3_client()

        qs = Camera.objects.all().order_by("id")
        if options["camera_id"]:
            qs = qs.filter(id=options["camera_id"])
        else:
            qs = qs.filter(is_active=True)

        if not qs.exists():
            raise CommandError("No cameras found for the selected filters")

        grand_total = IndexCounts()

        for camera in qs:
            counts = _index_camera(
                s3, camera, options, self.stdout.write, self.stderr.write
            )
            grand_total += counts
            self.stdout.write(
                self.style.SUCCESS(f"Camera {camera.id} done: {counts.summary()}")
            )

        self.stdout.write(
            self.style.SUCCESS(f"\nAll cameras done: {grand_total.summary()}")
        )
