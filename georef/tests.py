import datetime
import math

from django.core.exceptions import ValidationError
from django.db import ProgrammingError, connection, transaction
from django.test import TestCase

from georef import validation
from georef.constants import BELVEDERE_FRAME_NAME, ENU_SRID, PROJECT_SRID
from georef.crs import crs_definition
from georef.enu import from_enu, to_enu
from georef.examples import enu_transform
from georef.models import ReferenceFrame
from image_index.models import Camera
from surveys.models import Measurement, Point, Survey

# A point in the middle of the glacier, EPSG:32632, ellipsoidal height.
SAMPLE_EAST, SAMPLE_NORTH, SAMPLE_H = 416012.633, 5089977.534, 2054.660

# A move of N grid metres is slightly more than N metres on the ground: the
# UTM scale factor (~0.99969 here) and 2 km of altitude (1 + h/N) each stretch
# it, together by ~0.64 mm/m. Removing exactly that is the point of the frame,
# so the trigger tests only sanity-check the magnitude; exactness is pinned by
# the round-trip tests and `check_rigid_motion`.
GROUND_STRETCH_BAND = (1.0, 1.002)


def pipeline_value(pipeline: str, key: str) -> float:
    """Read a `+key=value` parameter out of a PROJ pipeline string."""
    prefix = f"+{key}="
    token = next(t for t in pipeline.split() if t.startswith(prefix))
    return float(token.removeprefix(prefix))


class ReferenceFrameTests(TestCase):
    """The frame row itself: it must exist on a freshly migrated database."""

    def setUp(self) -> None:
        self.frame = ReferenceFrame.objects.get(srid=ENU_SRID)

    def test_frame_created_by_migration(self) -> None:
        self.assertEqual(self.frame.name, BELVEDERE_FRAME_NAME)
        self.assertEqual(self.frame.origin_mark, "D12")
        self.assertEqual(self.frame.base_srid, PROJECT_SRID)
        self.assertEqual((self.frame.x_off, self.frame.y_off), (10000.0, 10000.0))

    def test_pipeline_is_generated_from_parameters(self) -> None:
        pipeline = self.frame.proj_pipeline
        self.assertTrue(pipeline.startswith("+proj=pipeline"))
        self.assertIn("+proj=topocentric", pipeline)

        # Every parameter must survive into the pipeline at full double
        # precision. Compared as parsed floats, not substrings: Postgres
        # renders 10000.0 as "10000" while Python renders it as "10000.0".
        for key, expected in (
            ("lat_0", self.frame.lat_0),
            ("lon_0", self.frame.lon_0),
            ("h_0", self.frame.h_0),
            ("xoff", self.frame.x_off),
            ("yoff", self.frame.y_off),
            ("zoff", self.frame.z_off),
        ):
            with self.subTest(parameter=key):
                self.assertEqual(pipeline_value(pipeline, key), expected)

    def test_pipeline_follows_parameter_changes(self) -> None:
        self.frame.h_0 = 2222.0
        self.frame.save()
        self.frame.refresh_from_db()
        self.assertIn("+h_0=2222", self.frame.proj_pipeline)

    def test_spatial_ref_sys_row_registered(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT auth_name, proj4text, srtext FROM spatial_ref_sys WHERE srid = %s",
                [ENU_SRID],
            )
            auth_name, proj4text, srtext = cursor.fetchone()

        self.assertEqual(auth_name, "BELVEDERE")
        # PROJ cannot compose geographic -> topocentric as a CRS, so the
        # registered definition is the orthographic display equivalent.
        self.assertIn("+proj=ortho", proj4text)
        self.assertIn("+x_0=10000", proj4text)
        self.assertIn("Orthographic", srtext)
        self.assertIn("georef_from_enu", srtext)


class FreezeGuardTests(TestCase):
    """A frozen frame is immutable — corrections mean a new SRID."""

    def setUp(self) -> None:
        ReferenceFrame.objects.filter(srid=ENU_SRID).update(frozen=True)

    def tearDown(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE georef_reference_frame SET frozen = false WHERE srid = %s",
                [ENU_SRID],
            )

    def test_django_save_rejects_origin_change(self) -> None:
        frame = ReferenceFrame.objects.get(srid=ENU_SRID)
        frame.h_0 += 1.0
        with self.assertRaises(ValidationError):
            frame.save()

    def test_database_trigger_rejects_origin_change(self) -> None:
        # plpgsql RAISE EXCEPTION is SQLSTATE P0001, which Django surfaces as
        # ProgrammingError.
        with self.assertRaises(ProgrammingError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE georef_reference_frame SET lat_0 = lat_0 + 0.001 "
                    "WHERE srid = %s",
                    [ENU_SRID],
                )

    def test_database_trigger_rejects_delete(self) -> None:
        with self.assertRaises(ProgrammingError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM georef_reference_frame WHERE srid = %s", [ENU_SRID]
                )

    def test_unrelated_fields_still_editable(self) -> None:
        frame = ReferenceFrame.objects.get(srid=ENU_SRID)
        frame.notes = "audited 2026"
        frame.save()
        self.assertEqual(
            ReferenceFrame.objects.get(srid=ENU_SRID).notes, "audited 2026"
        )


class PyprojTests(TestCase):
    """The Python-side view of the frame, driven by pyproj."""

    def setUp(self) -> None:
        self.frame = ReferenceFrame.objects.get(srid=ENU_SRID)

    def test_origin_maps_to_false_origin(self) -> None:
        e, n, u = to_enu(self.frame, self.frame.lon_0, self.frame.lat_0, self.frame.h_0)
        self.assertAlmostEqual(e, self.frame.x_off, places=6)
        self.assertAlmostEqual(n, self.frame.y_off, places=6)
        self.assertAlmostEqual(u, self.frame.z_off, places=6)

    def test_round_trip(self) -> None:
        lon, lat, h = 7.92, 45.97, 1950.0
        back = from_enu(self.frame, *to_enu(self.frame, lon, lat, h))
        self.assertAlmostEqual(back[0], lon, places=11)
        self.assertAlmostEqual(back[1], lat, places=11)
        self.assertAlmostEqual(back[2], h, places=6)


class CrsDefinitionTests(TestCase):
    """The CRS text handed to QGIS, including its embedded caveat."""

    def setUp(self) -> None:
        self.frame = ReferenceFrame.objects.get(srid=ENU_SRID)

    def test_proj4_keeps_full_precision(self) -> None:
        proj4text, _ = crs_definition(self.frame)
        self.assertIn("+proj=ortho", proj4text)
        # pyproj's own to_proj4() would round these; ours must not
        self.assertIn(f"+lat_0={self.frame.lat_0}", proj4text)
        self.assertIn(f"+x_0={self.frame.x_off}", proj4text)

    def test_remark_survives_into_wkt(self) -> None:
        """The caveat has to travel with the CRS, not just live in the docs."""
        _, srtext = crs_definition(self.frame)
        self.assertIn("REMARK[", srtext)
        self.assertIn("NOT interchangeable", srtext)
        self.assertIn("georef_from_enu()", srtext)
        self.assertIn(self.frame.proj_pipeline, srtext)
        self.assertIn(self.frame.name, srtext)


class ExampleScriptTests(TestCase):
    """`georef/examples/enu_transform.py` hardcodes the frame, so it can rot."""

    def test_pipeline_matches_the_database(self) -> None:
        frame = ReferenceFrame.objects.get(srid=ENU_SRID)
        self.assertEqual(enu_transform.PIPELINE, frame.proj_pipeline)

    def test_documented_values_still_hold(self) -> None:
        self.assertEqual(enu_transform.self_test(), 0)

    def test_agrees_with_the_database(self) -> None:
        source = (SAMPLE_EAST, SAMPLE_NORTH, SAMPLE_H)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ST_X(p), ST_Y(p), ST_Z(p) FROM ("
                "  SELECT georef_to_enu("
                "      ST_SetSRID(ST_MakePoint(%s, %s, %s), %s), %s) AS p"
                ") t",
                [*source, PROJECT_SRID, ENU_SRID],
            )
            expected = cursor.fetchone()

        for axis, got, want in zip(
            "ENU", enu_transform.to_enu(*source), expected, strict=True
        ):
            with self.subTest(axis=axis):
                self.assertAlmostEqual(got, want, places=6)


class FrameValidationTests(TestCase):
    """The suite that gates `freeze_reference_frame`, run against the DB."""

    #: Every check `run_checks` is expected to report. Listed explicitly so a
    #: check cannot quietly vanish during a refactor.
    EXPECTED_CHECKS = {
        "pipeline generated",
        "origin identity",
        "origin mark agreement",
        "axis convention",
        "round trip",
        "matches pyproj",
        "rigid motion (scale)",
        "positivity",
        "ortho display CRS",
        "pipeline route equivalence",
        "materialised up to date",
        "CRS definition current",
    }

    @classmethod
    def setUpTestData(cls) -> None:
        cls.results = validation.run_checks()

    def test_every_check_passes(self) -> None:
        for result in self.results:
            with self.subTest(check=result.name):
                self.assertTrue(result.passed, result.detail)

    def test_suite_is_complete(self) -> None:
        self.assertEqual({result.name for result in self.results}, self.EXPECTED_CHECKS)


class EnuTriggerTests(TestCase):
    """The materialised columns must stay in step with their sources."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.survey = Survey.objects.create(date=datetime.date(2026, 7, 1), year=2026)
        cls.point = Point.objects.create(label="TST1", is_fixed=False, active=True)

    def test_measurement_geom_enu_filled_on_insert(self) -> None:
        measurement = Measurement.objects.create(
            point=self.point,
            survey=self.survey,
            east=SAMPLE_EAST,
            north=SAMPLE_NORTH,
            h=SAMPLE_H,
        )
        measurement.refresh_from_db()
        self.assertIsNotNone(measurement.geom_enu)
        self.assertEqual(measurement.geom_enu.srid, ENU_SRID)
        self.assertTrue(9000 < measurement.geom_enu.x < 11000)
        self.assertTrue(9000 < measurement.geom_enu.y < 12000)
        self.assertTrue(measurement.geom_enu.hasz)
        # neutral height offset: U is lifted clear of zero, and is not an altitude
        self.assertTrue(700 < measurement.geom_enu.z < 1200)

    def test_measurement_geom_enu_follows_east(self) -> None:
        """Moving the source coordinates must move the ENU geometry.

        The shift is *not* exactly 10 m: `east` is a UTM **grid** coordinate,
        so 10 grid metres are ~10.006 m of true ground distance here.
        """
        measurement = Measurement.objects.create(
            point=self.point,
            survey=self.survey,
            east=SAMPLE_EAST,
            north=SAMPLE_NORTH,
            h=SAMPLE_H,
        )
        measurement.refresh_from_db()
        before = measurement.geom_enu

        measurement.east = SAMPLE_EAST + 10.0
        measurement.save()
        measurement.refresh_from_db()
        after = measurement.geom_enu

        moved = math.dist((before.x, before.y, before.z), (after.x, after.y, after.z))
        low, high = GROUND_STRETCH_BAND
        self.assertGreater(moved / 10.0, low)
        self.assertLess(moved / 10.0, high)

    def test_measurement_geom_enu_updated_value_round_trips(self) -> None:
        measurement = Measurement.objects.create(
            point=self.point,
            survey=self.survey,
            east=SAMPLE_EAST,
            north=SAMPLE_NORTH,
            h=SAMPLE_H,
        )
        measurement.east = SAMPLE_EAST + 10.0
        measurement.save()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ST_X(p) FROM ("
                "  SELECT georef_from_enu(geom_enu, %s) AS p"
                "  FROM measurements WHERE id = %s"
                ") t",
                [PROJECT_SRID, measurement.id],
            )
            self.assertAlmostEqual(cursor.fetchone()[0], SAMPLE_EAST + 10.0, places=6)

    def test_measurement_geom_enu_round_trips_to_source(self) -> None:
        measurement = Measurement.objects.create(
            point=self.point,
            survey=self.survey,
            east=SAMPLE_EAST,
            north=SAMPLE_NORTH,
            h=SAMPLE_H,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ST_X(p), ST_Y(p), ST_Z(p) FROM ("
                "  SELECT georef_from_enu(geom_enu, %s) AS p"
                "  FROM measurements WHERE id = %s"
                ") t",
                [PROJECT_SRID, measurement.id],
            )
            east, north, h = cursor.fetchone()

        self.assertAlmostEqual(east, SAMPLE_EAST, places=6)
        self.assertAlmostEqual(north, SAMPLE_NORTH, places=6)
        self.assertAlmostEqual(h, SAMPLE_H, places=6)

    def test_camera_location_enu_filled_from_location(self) -> None:
        camera = Camera.objects.create(
            camera_name="Test cam",
            slug="test-cam",
            s3_bucket="belvedere-images",
            s3_prefix="test/",
            easting=SAMPLE_EAST,
            northing=SAMPLE_NORTH,
            elevation=SAMPLE_H,
        )
        camera.refresh_from_db()
        self.assertIsNotNone(camera.location_enu)
        self.assertEqual(camera.location_enu.srid, ENU_SRID)
        self.assertTrue(9000 < camera.location_enu.x < 11000)
        self.assertTrue(700 < camera.location_enu.z < 1200)

    def test_camera_location_enu_follows_location(self) -> None:
        camera = Camera.objects.create(
            camera_name="Moving cam",
            slug="moving-cam",
            s3_bucket="belvedere-images",
            s3_prefix="moving/",
            easting=SAMPLE_EAST,
            northing=SAMPLE_NORTH,
            elevation=SAMPLE_H,
        )
        camera.refresh_from_db()
        before = camera.location_enu

        camera.northing = SAMPLE_NORTH + 25.0
        camera.save()
        camera.refresh_from_db()
        after = camera.location_enu

        moved = math.dist((before.x, before.y, before.z), (after.x, after.y, after.z))
        low, high = GROUND_STRETCH_BAND
        self.assertGreater(moved / 25.0, low)
        self.assertLess(moved / 25.0, high)


class ReferenceSystemsApiTests(TestCase):
    """GET /georef/reference-systems/ (the frame row comes from georef/0003)."""

    def test_lists_crs_heights_and_frames(self):
        import pyproj
        from django.urls import reverse

        response = self.client.get(reverse("georef:reference-systems"))  # public
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["project_crs"], f"EPSG:{PROJECT_SRID}")
        names = {c["code"]: c["name"] for c in data["crs"]}
        self.assertEqual(names["EPSG:7791"], "RDN2008 / UTM zone 32N")
        self.assertIn("EPSG:6704", names)
        self.assertEqual(data["heights"][1]["reference"], "ITALGEO05 geoid")

        (frame,) = [f for f in data["local_frames"] if f["srid"] == ENU_SRID]
        self.assertEqual(frame["origin_mark"], "D12")
        # The pipeline, used as the response describes it, maps the origin
        # (lon, lat in degrees, ellipsoidal h) onto the false origin.
        pipeline = pyproj.Transformer.from_pipeline(frame["proj_pipeline"])
        origin = frame["origin"]
        east, north, up = pipeline.transform(
            origin["lon"], origin["lat"], origin["h_ellipsoidal"]
        )
        false_origin = frame["false_origin"]
        self.assertAlmostEqual(east, false_origin["east"], places=6)
        self.assertAlmostEqual(north, false_origin["north"], places=6)
        self.assertAlmostEqual(up, false_origin["up"], places=6)
        # The ortho display definition is deliberately not served.
        self.assertNotIn("+proj=ortho", response.content.decode())
