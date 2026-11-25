import logging
import os
import uuid

from django.conf import settings
from django.contrib.gis.db import models
from django.core.files.storage import Storage
from django.db import transaction
from django.db.models.functions import Lower, Trim
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone

from georef.constants import ENU_SRID, PROJECT_SRID
from image_index.s3_utils import build_s3_storage
from surveys.sql import COLUMN_COMMENTS as DOC
from surveys.sql import TABLE_COMMENTS

logger = logging.getLogger(__name__)

# ================ Survey and Instrument Models ================


class Survey(models.Model):
    id = models.AutoField(primary_key=True)
    date = models.DateField(blank=True, null=True, db_comment=DOC["date"])
    year = models.BigIntegerField(blank=True, null=True, db_comment=DOC["year"])
    notes = models.CharField(
        max_length=254, blank=True, null=True, db_comment=DOC["notes"]
    )

    class Meta:
        db_table = "surveys"
        db_table_comment = TABLE_COMMENTS["surveys"]

    def __str__(self):
        return f"Survey {self.id} - {self.date}"


class Instrument(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=254, blank=True, null=True)
    type = models.CharField(max_length=254, blank=True, null=True)
    specificat = models.CharField(max_length=254, blank=True, null=True)
    brand = models.CharField(max_length=254, blank=True, null=True)
    year_of_m = models.BigIntegerField(blank=True, null=True)
    reseller = models.CharField(max_length=254, blank=True, null=True)

    class Meta:
        db_table = "instruments"

    def __str__(self):
        return f"Instrument {self.id} - {self.name}"


class SurveyHasInstrument(models.Model):
    id = models.AutoField(primary_key=True)
    fk_surveys = models.ForeignKey(
        Survey, models.DO_NOTHING, db_column="fk_surveys", blank=True, null=True
    )
    fk_instrum = models.ForeignKey(
        Instrument, models.DO_NOTHING, db_column="fk_instrum", blank=True, null=True
    )

    class Meta:
        db_table = "surveys_has_instruments"
        verbose_name = "Survey-Instrument association"


class Flight(models.Model):
    id = models.AutoField(primary_key=True)
    fk_surveys = models.ForeignKey(Survey, models.DO_NOTHING, db_column="fk_surveys")
    average_height = models.DecimalField(
        max_digits=10, decimal_places=5, blank=True, null=True
    )
    n_images = models.BigIntegerField(blank=True, null=True)
    n_controlpoints = models.BigIntegerField(blank=True, null=True)
    n_checkpoints = models.BigIntegerField(blank=True, null=True)
    average_gsd = models.DecimalField(
        max_digits=10, decimal_places=5, blank=True, null=True
    )
    camera_name = models.CharField(max_length=254, blank=True, null=True)
    focal_length = models.DecimalField(
        max_digits=10, decimal_places=5, blank=True, null=True
    )
    sensor_size = models.CharField(max_length=254, blank=True, null=True)
    image_size = models.CharField(max_length=254, blank=True, null=True)
    pixel_size = models.DecimalField(
        max_digits=10, decimal_places=5, blank=True, null=True
    )
    global_accuracy = models.DecimalField(
        max_digits=10, decimal_places=5, blank=True, null=True
    )
    x_accuracy = models.DecimalField(
        max_digits=10, decimal_places=5, blank=True, null=True
    )
    y_accuracy = models.DecimalField(
        max_digits=10, decimal_places=5, blank=True, null=True
    )
    z_accuracy = models.DecimalField(
        max_digits=10, decimal_places=5, blank=True, null=True
    )

    class Meta:
        db_table = "flights"

    def __str__(self):
        return f"Flight {self.id} - Survey {self.fk_surveys_id}"


# ================ GNSS Measurement Models ================


class Point(models.Model):
    id = models.AutoField(primary_key=True)
    label = models.CharField(
        max_length=45, blank=True, null=True, db_comment=DOC["label"]
    )
    active = models.BooleanField(blank=True, null=True, db_comment=DOC["active"])
    is_fixed = models.BooleanField(blank=True, null=True, db_comment=DOC["is_fixed"])
    ref_date = models.DateField(blank=True, null=True, db_comment=DOC["ref_date"])
    notes = models.CharField(
        max_length=512, blank=True, null=True, db_comment=DOC["notes"]
    )
    # Monograph texts (see import_monograph_texts). `db_default` keeps the
    # columns insertable from QGIS, which does not know about them.
    target = models.CharField(
        max_length=255, blank=True, default="", db_default="", db_comment=DOC["target"]
    )
    description = models.TextField(
        blank=True, default="", db_default="", db_comment=DOC["description"]
    )
    survey_mode = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_default="",
        db_comment=DOC["survey_mode"],
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        db_table = "points"
        db_table_comment = TABLE_COMMENTS["points"]
        constraints = [
            # The pre-existing unique index on `label` is case-sensitive, which
            # is how the 2026 import created D01BIS next to D01bis. Matches the
            # key `merge_duplicate_points` groups by.
            models.UniqueConstraint(
                Lower(Trim("label")), name="points_label_unique_ci"
            ),
        ]

    def __str__(self):
        return f"Point {self.label} (ID {self.id})"


class Measurement(models.Model):
    id = models.AutoField(primary_key=True)
    geom = models.PointField(
        srid=PROJECT_SRID, blank=True, null=True, db_comment=DOC["geom"]
    )
    east = models.FloatField(db_comment=DOC["east"])
    north = models.FloatField(db_comment=DOC["north"])
    h = models.FloatField(verbose_name="h (ellipsoidal)", db_comment=DOC["h"])
    # PROTECT, not DO_NOTHING: measurements are the irreplaceable data, so
    # deleting their point or survey must fail with a readable message rather
    # than letting the database raise a bare IntegrityError. To remove a point
    # together with its measurements, use the admin action on PointAdmin.
    point = models.ForeignKey(Point, models.PROTECT, db_column="point")
    survey = models.ForeignKey(Survey, models.PROTECT, db_column="survey")
    meas_date = models.DateField(blank=True, null=True, db_comment=DOC["meas_date"])
    std_east = models.FloatField(
        blank=True, null=True, verbose_name="std east", db_comment=DOC["std_east"]
    )
    std_north = models.FloatField(
        blank=True, null=True, verbose_name="std north", db_comment=DOC["std_north"]
    )
    std_h = models.FloatField(
        blank=True, null=True, verbose_name="std h", db_comment=DOC["std_h"]
    )
    meas_strategy = models.CharField(
        max_length=25, blank=True, null=True, db_comment=DOC["meas_strategy"]
    )
    meas_time = models.DateTimeField(blank=True, null=True, db_comment=DOC["meas_time"])
    notes = models.CharField(
        max_length=250, blank=True, null=True, db_comment=DOC["notes"]
    )
    lat = models.FloatField(blank=True, null=True, db_comment=DOC["lat"])
    lon = models.FloatField(blank=True, null=True, db_comment=DOC["lon"])
    h_orto = models.FloatField(blank=True, null=True, db_comment=DOC["h_orto"])
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    geom_enu = models.PointField(
        dim=3,
        srid=ENU_SRID,
        blank=True,
        null=True,
        editable=False,
        help_text=(
            "Position in the local ENU frame, filled by a database trigger "
            "from east/north/h. Read-only: edit the source coordinates instead."
        ),
        db_comment=DOC["geom_enu"],
    )

    # A realization mismatch between campaigns would look exactly like glacier
    # motion, so it is recorded rather than assumed. The SRID is not stored —
    # `geom` already carries it.
    datum_realization = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        choices=[
            ("RDN2008", "RDN2008 (ETRF2000, epoch 2008.0)"),
            ("ETRF2000", "ETRF2000"),
            ("ITRF2014", "ITRF2014"),
        ],
        help_text="Reference frame realization the campaign was processed in.",
        db_comment=DOC["datum_realization"],
    )
    height_type = models.CharField(
        max_length=16,
        blank=True,
        null=True,
        choices=[("ellipsoidal", "ellipsoidal"), ("orthometric", "orthometric")],
        help_text="Should be 'ellipsoidal' everywhere; h_orto is computed internally from h using ITALGEO05 geoid model.",
        db_comment=DOC["height_type"],
    )

    class Meta:
        db_table = "measurements"
        db_table_comment = TABLE_COMMENTS["measurements"]

    def __str__(self):
        return (
            f"Measurement {self.id} - Point {self.point_id} @ Survey {self.survey_id}"
        )


def photo_storage() -> Storage:
    """S3 storage of the measurement photos (private surveys bucket)."""
    return build_s3_storage(settings.S3_SURVEYS_BUCKET_NAME)


def measurement_photo_upload_to(instance: "MeasurementPhoto", filename: str) -> str:
    """S3 key `<prefix><measurement_id>/<uuid><ext>`.

    A new key per upload, so a replaced photo can never be served from a cache
    under the old name.
    """
    ext = os.path.splitext(filename)[1].lower() or ".jpg"
    return (
        f"{settings.S3_MEASUREMENT_PHOTOS_PREFIX}"
        f"{instance.measurement_id}/{uuid.uuid4().hex}{ext}"
    )


def _delete_photo_file(storage: Storage, name: str) -> None:
    """Delete an S3 object once the transaction commits.

    Never before: a rolled-back save must not lose a file that is still
    referenced. `robust` logs an S3 failure instead of failing the request.

    Only objects under this environment's prefix are deleted. A local copy of
    the database shares the bucket with production but uploads under `dev/`;
    without this, deleting or replacing a photo locally would delete the file
    production still uses.
    """
    if not name.startswith(settings.S3_MEASUREMENT_PHOTOS_PREFIX):
        logger.info("kept %s: outside %s", name, settings.S3_MEASUREMENT_PHOTOS_PREFIX)
        return
    transaction.on_commit(lambda: storage.delete(name), robust=True)


class MeasurementPhoto(models.Model):
    """One photo of a measurement; a measurement can have several."""

    id = models.AutoField(primary_key=True)
    measurement = models.ForeignKey(
        "Measurement",
        on_delete=models.CASCADE,
        related_name="photos",
        db_comment=DOC["measurement_id"],
    )
    image = models.ImageField(
        upload_to=measurement_photo_upload_to,
        storage=photo_storage,
        max_length=255,
        db_comment=DOC["image"],
    )
    file_name = models.CharField(
        max_length=254, blank=True, editable=False, db_comment=DOC["file_name"]
    )
    uploaded_at = models.DateTimeField(
        null=True, editable=False, db_comment=DOC["uploaded_at"]
    )

    class Meta:
        db_table = "measurement_photos"
        db_table_comment = TABLE_COMMENTS["measurement_photos"]
        verbose_name = "Measurement photo"
        ordering = ("measurement_id", "uploaded_at", "id")

    def save(self, *args, **kwargs) -> None:
        replaced = None
        if self.image and not self.image._committed:
            # Before upload_to renames it, the name is the uploaded file's.
            self.file_name = os.path.basename(self.image.name)[:254]
            self.uploaded_at = timezone.now()
            if self.pk:
                replaced = (
                    MeasurementPhoto.objects.filter(pk=self.pk)
                    .values_list("image", flat=True)
                    .first()
                )
        super().save(*args, **kwargs)
        if replaced and replaced != self.image.name:
            _delete_photo_file(self.image.storage, replaced)

    def get_absolute_url(self) -> str:
        return reverse("surveys:measurement-photo", args=[self.pk])

    def __str__(self):
        return f"Photo {self.pk} of measurement {self.measurement_id}"


@receiver(post_delete, sender=MeasurementPhoto)
def delete_photo_file_with_row(
    sender: type[MeasurementPhoto], instance: MeasurementPhoto, **kwargs
) -> None:
    """Remove the S3 object with its row, including cascades from Measurement."""
    if instance.image:
        _delete_photo_file(instance.image.storage, instance.image.name)


# ================ Survey Products and Volumes ================


class Product2D(models.Model):
    """Raster product of a survey (orthophoto, DSM, ...) served via WMS."""

    survey = models.ForeignKey(
        Survey,
        on_delete=models.PROTECT,
        related_name="products_2d",
    )
    data_type = models.CharField(
        max_length=64,
        help_text="Product type, e.g. ortofoto, dsm.",
    )
    file_format = models.CharField(max_length=32, blank=True, null=True)
    reference_system = models.CharField(max_length=254, blank=True, null=True)
    epsg = models.IntegerField(blank=True, null=True)
    proj4 = models.CharField(max_length=512, blank=True, null=True)
    bounding_box = models.CharField(max_length=254, blank=True, null=True)
    license = models.CharField(max_length=64, blank=True, null=True)
    pixel_size = models.FloatField(
        blank=True, null=True, help_text="Ground pixel size in metres."
    )
    n_bands = models.IntegerField(blank=True, null=True)
    wms_url = models.URLField(
        max_length=1024,
        blank=True,
        null=True,
        help_text="WMS endpoint URL on GeoServer.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "products_2d"
        verbose_name = "2D product"

    def __str__(self) -> str:
        return f"Product2D {self.pk} - {self.data_type} (survey {self.survey_id})"


class Product3D(models.Model):
    """3D product of a survey (point cloud, mesh, ...) with a direct URL."""

    survey = models.ForeignKey(
        Survey,
        on_delete=models.PROTECT,
        related_name="products_3d",
    )
    data_type = models.CharField(
        max_length=64,
        help_text="Product type, e.g. pointcloud, mesh.",
    )
    file_format = models.CharField(max_length=32, blank=True, null=True)
    reference_system = models.CharField(max_length=254, blank=True, null=True)
    epsg = models.IntegerField(blank=True, null=True)
    bounding_box = models.CharField(max_length=254, blank=True, null=True)
    license = models.CharField(max_length=64, blank=True, null=True)
    average_point_spacing = models.FloatField(
        blank=True, null=True, help_text="Average point spacing in metres."
    )
    n_points = models.BigIntegerField(blank=True, null=True)
    n_nodes = models.BigIntegerField(blank=True, null=True)
    n_scalar_fields = models.IntegerField(blank=True, null=True)
    url = models.URLField(
        max_length=1024,
        blank=True,
        null=True,
        help_text="Direct URL to the COPC point cloud (Zenodo, S3, etc.).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "products_3d"
        verbose_name = "3D product"

    def __str__(self) -> str:
        return f"Product3D {self.pk} - {self.data_type} (survey {self.survey_id})"


class Volume(models.Model):
    """Glacier volume variation between two consecutive surveys."""

    survey = models.ForeignKey(Survey, on_delete=models.PROTECT, related_name="volumes")
    survey_prev = models.ForeignKey(
        Survey, on_delete=models.PROTECT, related_name="volumes_as_previous"
    )
    dv = models.FloatField(help_text="Volume difference in cubic metres.")
    density = models.FloatField(blank=True, null=True, help_text="Density in kg/m³.")
    density_uncertainty = models.FloatField(blank=True, null=True)

    class Meta:
        db_table = "volumes"

    def __str__(self):
        return f"Volume {self.pk} - surveys {self.survey_prev_id}→{self.survey_id}"


# ================ Database views (read-only) ================
# Unmanaged models over the Postgres views created in migration 0011.
# Django never writes to them; any change to a view definition must go
# through a new migration.


class PointsMeasurement(models.Model):
    """Row of the `points_measurements` view (measurement + point + survey join)."""

    id = models.IntegerField(primary_key=True)
    label = models.CharField(max_length=45)
    point_id = models.IntegerField()
    is_active = models.BooleanField(null=True)
    is_fixed = models.BooleanField()
    east = models.FloatField()
    north = models.FloatField()
    h = models.FloatField(verbose_name="h (ellipsoidal)")
    h_orto = models.FloatField(null=True)
    lat = models.FloatField(null=True)
    lon = models.FloatField(null=True)
    survey_id = models.IntegerField()
    survey_date = models.DateField(null=True)
    survey_year = models.BigIntegerField(null=True)
    meas_date = models.DateField(null=True)
    meas_time = models.DateTimeField(null=True)
    meas_strategy = models.CharField(max_length=25, null=True)
    std_east = models.FloatField(null=True)
    std_north = models.FloatField(null=True)
    std_h = models.FloatField(null=True)

    class Meta:
        managed = False
        db_table = "points_measurements"
        verbose_name = "Points measurement (read-only view)"
        verbose_name_plural = "Points measurements (read-only view)"

    def __str__(self):
        return f"{self.label} @ {self.survey_year}"


class PointMovement(models.Model):
    """Row of the `point_movements` view: one moving point between two measurements.

    Horizontal `d`/`v` are the main values; `d_3d`/`v_3d` include the vertical
    part, i.e. glacier surface lowering. Velocities in m/yr, accelerations in
    m/yr2. `is_annual` marks pairs about a year apart; acceleration is only
    computed between consecutive annual pairs. Definition in `surveys/sql.py`.
    """

    id = models.IntegerField(primary_key=True)  # id of the later measurement
    point_id = models.IntegerField()
    label = models.CharField(max_length=45)
    survey_year = models.BigIntegerField(null=True, verbose_name="year (end)")
    date_prev = models.DateField(verbose_name="start date")
    date_fin = models.DateField(verbose_name="end date")
    dt = models.IntegerField(verbose_name="dt (days)")
    is_annual = models.BooleanField()
    geom_line = models.LineStringField(srid=PROJECT_SRID, null=True)
    d_e = models.FloatField(verbose_name="d_e (m)")
    d_n = models.FloatField(verbose_name="d_n (m)")
    d = models.FloatField(verbose_name="d (m)")
    d_h = models.FloatField(verbose_name="d_h (m)")
    d_3d = models.FloatField(verbose_name="d_3d (m)")
    v_e = models.FloatField(verbose_name="v_e (m/yr)")
    v_n = models.FloatField(verbose_name="v_n (m/yr)")
    v = models.FloatField(verbose_name="v (m/yr)")
    v_h = models.FloatField(verbose_name="v_h (m/yr)")
    v_3d = models.FloatField(verbose_name="v_3d (m/yr)")
    a_e = models.FloatField(null=True, verbose_name="a_e (m/yr²)")
    a_n = models.FloatField(null=True, verbose_name="a_n (m/yr²)")
    a = models.FloatField(null=True, verbose_name="a (m/yr²)")
    lat_prev = models.FloatField(null=True)
    lon_prev = models.FloatField(null=True)
    lat_fin = models.FloatField(null=True)
    lon_fin = models.FloatField(null=True)

    class Meta:
        managed = False
        db_table = "point_movements"
        verbose_name = "Point movement (read-only view)"
        verbose_name_plural = "Point movements (read-only view)"

    def __str__(self):
        return f"{self.label} {self.date_prev}→{self.date_fin}"


class ActivePoint(models.Model):
    """Row of the `active_points` view (latest measurement per active point)."""

    meas_id = models.IntegerField(primary_key=True)
    label = models.CharField(max_length=45)
    last_measure_date = models.DateField(null=True)
    east = models.FloatField()
    north = models.FloatField()
    h = models.FloatField(verbose_name="h (ellipsoidal)")
    is_fixed = models.BooleanField()

    class Meta:
        managed = False
        db_table = "active_points"
        verbose_name = "Active point (read-only view)"
        verbose_name_plural = "Active points (read-only view)"

    def __str__(self):
        return f"{self.label} ({self.last_measure_date})"


# =========== Unmanaged legacy tables (NOT modelled in Django) ==============

#   - scatter_points, scatter_measurements: legacy scatter manual displacement points kept in the DB for reference.
#   - layer_styles, qgis_projects: owned and managed by QGIS.
#   - raster.geoid_model: geoid raster sampled by the compute_measurement_geometry
#     trigger (maintained outside Django).
