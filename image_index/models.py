"""
Django models Image Index app.
"""

import logging
from enum import IntEnum
from pathlib import PurePosixPath
from urllib.parse import quote

from django.conf import settings
from django.contrib.gis.db import models
from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.fields import ArrayField

from georef.constants import ENU_SRID, PROJECT_SRID
from image_index.image_metadata import (
    FILENAME_DATETIME_RE,
    parse_datetime_from_exif_dict,
    parse_datetime_from_filename,
)

logger = logging.getLogger("belv")

# ================ Camera and Calibration Models ================


class CameraModel(IntEnum):
    SIMPLE_PINHOLE = 0  # f, cx, cy
    PINHOLE = 1  # fx, fy, cx, cy
    SIMPLE_RADIAL = 2  # f, cx, cy, k1
    RADIAL = 3  # f, cx, cy, k1, k2
    OPENCV = 4  # fx, fy, cx, cy, k1, k2, p1, p2
    OPENCV_FISHEYE = 5  # fx, fy, cx, cy, k1, k2, k3, k4
    FULL_OPENCV = 6  # fx, fy, cx, cy, k1, k2, p1, p2, k3, k4, k5, k6
    FOV = 7  # fx, fy, cx, cy, omega
    SIMPLE_RADIAL_FISHEYE = 8  # f, cx, cy, k1
    RADIAL_FISHEYE = 9  # f, cx, cy, k1, k2
    THIN_PRISM_FISHEYE = 10  # fx, fy, cx, cy, k1, k2, p1, p2, k3, k4, sx1, sy1

    METASHAPE = 11  # THIS IS A CUSTOM MODEL FOR METASHAPE CALIBRATION PARAMETERS (f, cx, cy, k1, k2, p1, p2, k3, k4, k5, k6) - NOT OFFICIALLY SUPPORTED BY COLMAP

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class Camera(models.Model):
    """Information about each time-lapse camera."""

    camera_name = models.CharField(
        max_length=255,
        help_text="Human-readable camera name shown in admin and reports.",
    )
    slug = models.SlugField(
        max_length=255,
        unique=True,
        help_text="Short stable identifier for the camera, e.g. 'cam-01-north'. Useful in URLs, scripts, and filters.",
    )

    # S3 storage information for indexing and access; these fields are required for active cameras to enable indexing.
    s3_bucket = models.CharField(
        max_length=255,
        help_text="Name of the S3 bucket where this camera stores its images.",
    )
    s3_prefix = models.CharField(
        max_length=512,
        help_text="Folder/prefix inside the bucket for this camera, e.g. 'camera-01/' or 'cam-a/'.",
    )
    s3_key_filter_regex = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text=(
            "Optional regex (re.search, applied to the full S3 object key) further "
            "restricting which objects under s3_prefix are indexed for this camera. "
            "Leave blank to index every object under the prefix."
        ),
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Whether this camera is active and should be indexed from S3. Inactive cameras will be ignored by the indexing command.",
    )

    serial_number = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
        help_text="Optional hardware serial number of the camera.",
    )
    model = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Camera body model, e.g. 'Canon EOS 2000D'.",
    )
    lens = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Lens model or description.",
    )
    focal_length_mm = models.FloatField(
        null=True,
        blank=True,
        help_text="Nominal focal length in millimeters.",
    )
    sensor_width = models.IntegerField(
        null=True,
        blank=True,
        help_text="Sensor width in pixels.",
    )
    sensor_height = models.IntegerField(
        null=True,
        blank=True,
        help_text="Sensor height in pixels.",
    )
    resolution_Mpx = models.FloatField(
        null=True,
        blank=True,
        help_text="Sensor resolution in megapixels",
    )
    sensor_width_mm = models.FloatField(
        null=True,
        blank=True,
        help_text="Physical sensor width in millimeters.",
    )
    sensor_height_mm = models.FloatField(
        null=True,
        blank=True,
        help_text="Physical sensor height in millimeters.",
    )
    pixel_size_um = models.FloatField(
        null=True,
        blank=True,
        help_text="Physical pixel size in micrometers.",
    )
    gsd = models.FloatField(
        null=True,
        blank=True,
        help_text="Average ground sampling distance in cm/pixel.",
    )
    easting = models.FloatField(
        null=True,
        blank=True,
        help_text="Camera easting in the project CRS, usually EPSG:7791.",
    )
    northing = models.FloatField(
        null=True,
        blank=True,
        help_text="Camera northing in the project CRS, usually EPSG:7791.",
    )
    elevation = models.FloatField(
        null=True,
        blank=True,
        help_text="Camera elevation in meters in the project CRS.",
    )
    epsg_code = models.IntegerField(
        default=PROJECT_SRID,
        help_text="EPSG code of the coordinate reference system used for location fields.",
    )
    installation_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date when the camera was installed in the field.",
    )
    notes = models.TextField(
        null=True,
        blank=True,
        help_text="Optional free-text notes about installation, maintenance, or configuration.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    location = gis_models.PointField(
        dim=3,
        srid=PROJECT_SRID,
        null=True,
        blank=True,
        help_text="3D camera location as a PostGIS point geometry in the project CRS.",
    )
    location_enu = gis_models.PointField(
        dim=3,
        srid=ENU_SRID,
        null=True,
        blank=True,
        editable=False,
        help_text=(
            "Camera location in the local ENU frame, filled by a database "
            "trigger from `location`. Read-only: edit `location` instead."
        ),
    )

    class Meta:
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["s3_bucket", "s3_prefix"]),
        ]

    def __str__(self):
        return self.camera_name


class CameraCalibration(models.Model):
    """Camera interior and exterior orientation parameters."""

    camera = models.ForeignKey(
        Camera,
        on_delete=models.CASCADE,
        related_name="calibrations",
        help_text="Camera to which this calibration belongs.",
    )
    calibration_date = models.DateTimeField(
        help_text="Date and time when this calibration became valid or was computed.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this is the currently active calibration for the camera.",
    )
    model_id = models.IntegerField(
        choices=CameraModel.choices(),
        help_text="Numeric identifier of the camera model used for calibration parameters.",
    )
    model_name = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="Optional human-readable camera model name, e.g. OPENCV or METASHAPE.",
    )
    image_width_px = models.IntegerField(help_text="Calibration image width in pixels.")
    image_height_px = models.IntegerField(
        help_text="Calibration image height in pixels."
    )

    intrinsic_params = ArrayField(
        models.FloatField(),
        help_text="Ordered list of intrinsic calibration parameters according to the selected model.",
    )

    rotation_quaternion = ArrayField(
        models.FloatField(),
        size=4,
        null=True,
        blank=True,
        help_text="Optional camera rotation as quaternion [qw, qx, qy, qz].",
    )
    translation_vector = ArrayField(
        models.FloatField(),
        size=3,
        null=True,
        blank=True,
        help_text="Optional camera translation vector [tx, ty, tz].",
    )

    notes = models.TextField(
        null=True,
        blank=True,
        help_text="Optional notes about the calibration procedure or source.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["camera"],
                condition=models.Q(is_active=True),
                name="unique_active_calibration_per_camera",
            )
        ]

    def __str__(self):
        return f"{self.camera.camera_name} calibration {self.calibration_date:%Y-%m-%d}"

    def get_intrinsics_dict(self):
        """Convert the intrinsic parameters array to a dictionary with named parameters."""
        if self.model_id == CameraModel.SIMPLE_PINHOLE:
            return {
                "f": self.intrinsic_params[0],
                "cx": self.intrinsic_params[1],
                "cy": self.intrinsic_params[2],
            }
        elif self.model_id == CameraModel.PINHOLE:
            return {
                "fx": self.intrinsic_params[0],
                "fy": self.intrinsic_params[1],
                "cx": self.intrinsic_params[2],
                "cy": self.intrinsic_params[3],
            }
        elif self.model_id == CameraModel.SIMPLE_RADIAL:
            return {
                "f": self.intrinsic_params[0],
                "cx": self.intrinsic_params[1],
                "cy": self.intrinsic_params[2],
                "k1": self.intrinsic_params[3],
            }
        elif self.model_id == CameraModel.RADIAL:
            return {
                "f": self.intrinsic_params[0],
                "cx": self.intrinsic_params[1],
                "cy": self.intrinsic_params[2],
                "k1": self.intrinsic_params[3],
                "k2": self.intrinsic_params[4],
            }
        elif self.model_id == CameraModel.OPENCV:
            return {
                "fx": self.intrinsic_params[0],
                "fy": self.intrinsic_params[1],
                "cx": self.intrinsic_params[2],
                "cy": self.intrinsic_params[3],
                "k1": self.intrinsic_params[4],
                "k2": self.intrinsic_params[5],
                "p1": self.intrinsic_params[6],
                "p2": self.intrinsic_params[7],
            }
        return {"params": self.intrinsic_params}


# ================ Image Models ================


class Image(models.Model):
    """Metadata for each image acquired by the cameras."""

    FILENAME_DATETIME_RE = FILENAME_DATETIME_RE

    camera = models.ForeignKey(
        "image_index.Camera",
        on_delete=models.CASCADE,
        related_name="images",
        help_text="Camera that acquired or owns this image.",
    )

    bucket = models.CharField(
        max_length=255,
        help_text="S3 bucket containing the image object.",
    )
    object_key = models.CharField(
        max_length=1024,
        help_text="Full S3 object key inside the bucket, including folders/prefixes.",
    )
    filename = models.CharField(
        max_length=255,
        help_text="Basename of the image file, usually derived from the object key.",
    )
    s3_etag = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        db_index=True,
        help_text="S3 ETag returned by the object listing; useful for change detection.",
    )
    s3_last_modified = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Timestamp of the last modification reported by S3.",
    )
    file_size_bytes = models.BigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Object size in bytes as reported by S3.",
    )

    datetime = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Acquisition timestamp of the image, from EXIF, filename, or external processing.",
    )
    width_px = models.IntegerField(
        null=True,
        blank=True,
        help_text="Image width in pixels.",
    )
    height_px = models.IntegerField(
        null=True,
        blank=True,
        help_text="Image height in pixels.",
    )
    mime_type = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="MIME type of the object, e.g. image/jpeg.",
    )
    exif_data = models.JSONField(
        null=True,
        blank=True,
        help_text="Parsed EXIF metadata stored as JSON.",
    )
    rotation = models.IntegerField(
        default=0,
        help_text="Display rotation in degrees derived from EXIF orientation, usually 0, 90, 180, or 270.",
    )

    preview_object_key = models.CharField(
        max_length=1024,
        null=True,
        blank=True,
        help_text="S3 key of the pre-generated preview (640×480 JPEG with watermark). Null if not yet generated.",
    )
    thumbnail_object_key = models.CharField(
        max_length=1024,
        null=True,
        blank=True,
        help_text="S3 key of the pre-generated thumbnail (160×120 JPEG). Null if not yet generated.",
    )

    label = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Optional tag or label for grouping or review.",
    )

    is_indexed = models.BooleanField(
        default=True,
        help_text="Whether the image has been successfully indexed from S3.",
    )
    indexed_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when this database row was first created during indexing.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when this database row was last updated.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bucket", "object_key"],
                name="unique_bucket_object_key",
            ),
        ]
        indexes = [
            models.Index(fields=["camera", "datetime"]),
            models.Index(fields=["camera", "s3_last_modified"]),
            models.Index(fields=["bucket", "object_key"]),
        ]

    @property
    def file_path(self):
        endpoint = (getattr(settings, "S3_ENDPOINT_URL", "") or "s3").rstrip("/")
        if not endpoint or not self.bucket or not self.object_key:
            return ""

        encoded_key = quote(self.object_key, safe="/")
        return f"{endpoint}/{self.bucket}/{encoded_key}"

    @property
    def file_name(self):
        return self.filename or PurePosixPath(self.object_key).name

    @property
    def file_size_mb(self) -> float | None:
        if self.file_size_bytes is not None:
            return round(self.file_size_bytes / (1024 * 1024), 2)
        return None

    @property
    def file_size_human(self) -> str:
        size_bytes = self.file_size_bytes
        if size_bytes is None:
            return "N/A"

        for unit in ["B", "KB", "MB", "GB"]:
            if size_bytes < 1024.0:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} TB"

    def extract_datetime_from_exif(self):
        return parse_datetime_from_exif_dict(self.exif_data)

    def extract_datetime_from_filename(self):
        return parse_datetime_from_filename(self.filename or self.file_name)

    def extract_datetime(self):
        return (
            self.extract_datetime_from_exif() or self.extract_datetime_from_filename()
        )

    def save(self, *args, **kwargs):
        if self.object_key and not self.filename:
            self.filename = PurePosixPath(self.object_key).name

        if self.exif_data and isinstance(self.exif_data, dict):
            orientation = self.exif_data.get("Orientation") or self.exif_data.get(
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
            self.rotation = orientation_map.get(orientation, 0)

        if self.datetime is None:
            self.datetime = self.extract_datetime()

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.camera.camera_name} / {self.filename}"
