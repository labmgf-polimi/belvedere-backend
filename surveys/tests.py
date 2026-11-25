import datetime

from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from surveys.models import (
    ActivePoint,
    Measurement,
    Point,
    PointsMeasurement,
    PointsMovementFiltered,
    PointsMovementRaw,
    Survey,
)


class DatabaseViewTests(TestCase):
    """The migration-managed views must exist on a freshly migrated database
    and be queryable through their unmanaged models."""

    VIEW_MODELS = (
        PointsMeasurement,
        PointsMovementRaw,
        PointsMovementFiltered,
        ActivePoint,
    )

    def test_views_exist_and_are_queryable(self):
        for model in self.VIEW_MODELS:
            self.assertEqual(model.objects.count(), 0)


class SurveysApiTests(APITestCase):
    """Tests for /surveys/years/, /surveys/measurements/ and the velocity endpoint."""

    @classmethod
    def setUpTestData(cls):
        cls.survey_2015 = Survey.objects.create(
            id=1, date=datetime.date(2015, 7, 20), year=2015
        )
        cls.survey_2016 = Survey.objects.create(
            id=2,
            date=datetime.date(2016, 7, 19),
            year=2016,  # dt = 365 days
        )
        cls.survey_2016b = Survey.objects.create(
            id=3,
            date=datetime.date(2016, 10, 27),
            year=2016,  # dt = 100 days
        )
        cls.empty_survey = Survey.objects.create(
            id=4, date=datetime.date(2020, 7, 1), year=2020
        )
        cls.point = Point.objects.create(id=1, label="D01", is_fixed=False, active=True)
        cls.fixed_point = Point.objects.create(
            id=2, label="FIX1", is_fixed=True, active=True
        )
        # D01 moves by (3, 4, 0) m between 2015 and 2016 -> d = 5 m over 365 days
        cls.m1 = Measurement.objects.create(
            id=1,
            point=cls.point,
            survey=cls.survey_2015,
            east=100.0,
            north=200.0,
            h=50.0,
        )
        cls.m2 = Measurement.objects.create(
            id=2,
            point=cls.point,
            survey=cls.survey_2016,
            east=103.0,
            north=204.0,
            h=50.0,
        )
        # short-gap measurement (dt = 100 days, must be excluded from velocity)
        cls.m3 = Measurement.objects.create(
            id=3,
            point=cls.point,
            survey=cls.survey_2016b,
            east=104.0,
            north=205.0,
            h=50.0,
        )
        cls.m_fixed = Measurement.objects.create(
            id=4,
            point=cls.fixed_point,
            survey=cls.survey_2015,
            east=0.0,
            north=0.0,
            h=0.0,
        )

    def test_years_distinct_sorted_only_with_measurements(self):
        response = self.client.get(reverse("surveys:years"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [2015, 2016])

    def test_measurements_shape_matches_legacy_view(self):
        response = self.client.get(reverse("surveys:measurements"), {"year": 2015})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        record = next(r for r in data if r["label"] == "D01")
        expected_fields = {
            "id", "point_id", "label", "is_fixed", "east", "north", "h",
            "h_orto", "lat", "lon", "survey_id", "survey_date", "survey_year",
            "meas_date", "meas_time", "meas_strategy", "std_east", "std_north", "std_h",
        }  # fmt: skip
        self.assertEqual(set(record), expected_fields)
        self.assertEqual(record["point_id"], 1)
        self.assertEqual(record["survey_year"], 2015)
        self.assertEqual(record["survey_date"], "2015-07-20")
        self.assertEqual(record["east"], 100.0)

    def test_measurements_is_fixed_filter(self):
        response = self.client.get(
            reverse("surveys:measurements"), {"year": 2015, "is_fixed": "false"}
        )
        labels = [r["label"] for r in response.json()]
        self.assertEqual(labels, ["D01"])

    def test_velocity_math_and_dt_window(self):
        response = self.client.get(reverse("surveys:point-velocity", args=["D01"]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # only the 365-day pair survives the 280 < dt < 400 window
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["survey_year"], 2016)
        self.assertEqual(data[0]["survey_date_fin"], "2016-07-19")
        self.assertAlmostEqual(data[0]["v"], 5.0 / 365.0)

    def test_velocity_fixed_point_returns_empty(self):
        response = self.client.get(reverse("surveys:point-velocity", args=["FIX1"]))
        self.assertEqual(response.json(), [])

    def test_velocity_unknown_label_returns_empty(self):
        response = self.client.get(reverse("surveys:point-velocity", args=["NOPE"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


class MergeDuplicatePointsTests(TestCase):
    """Case-duplicate labels split a stake's history and cost it its movement."""

    @classmethod
    def setUpTestData(cls):
        # The command exists to clean up data that predates
        # `points_label_unique_ci`, so the fixtures need that earlier state.
        # Dropping the index here is undone by the test transaction rollback.
        with connection.cursor() as cursor:
            cursor.execute("DROP INDEX IF EXISTS points_label_unique_ci")

        cls.survey_a = Survey.objects.create(date=datetime.date(2025, 7, 20), year=2025)
        cls.survey_b = Survey.objects.create(date=datetime.date(2026, 7, 25), year=2026)

        cls.original = Point.objects.create(
            label="D01bis",
            active=True,
            is_fixed=True,
            ref_date=datetime.date(2017, 10, 5),
        )
        cls.duplicate = Point.objects.create(
            label="D01BIS",
            active=True,
            is_fixed=False,
            notes="Automatically created during measurement CSV import",
        )
        Measurement.objects.create(
            point=cls.original,
            survey=cls.survey_a,
            east=416000.0,
            north=5090000.0,
            h=2100.0,
        )
        Measurement.objects.create(
            point=cls.duplicate,
            survey=cls.survey_b,
            east=416003.0,
            north=5090004.0,
            h=2099.0,
        )
        # a genuinely new point with no twin must be left alone
        cls.untouched = Point.objects.create(
            label="D38BIS", active=True, is_fixed=False
        )

    def _run(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("merge_duplicate_points", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_without_changing_anything(self):
        output = self._run()
        self.assertIn("D01bis", output)
        self.assertIn("dry run", output)
        self.assertTrue(Point.objects.filter(pk=self.duplicate.pk).exists())
        self.assertEqual(Measurement.objects.filter(point=self.original).count(), 1)

    def test_dry_run_flags_differing_attributes(self):
        # the canonical row is is_fixed=True, the duplicate is False
        self.assertIn("differs on", self._run())

    def test_apply_repoints_measurements_and_removes_the_duplicate(self):
        self._run("--apply")
        self.assertFalse(Point.objects.filter(pk=self.duplicate.pk).exists())
        self.assertEqual(Measurement.objects.filter(point=self.original).count(), 2)
        # canonical attributes survive
        self.original.refresh_from_db()
        self.assertTrue(self.original.is_fixed)
        self.assertEqual(self.original.ref_date, datetime.date(2017, 10, 5))

    def test_point_without_a_twin_is_untouched(self):
        self._run("--apply")
        self.assertTrue(Point.objects.filter(pk=self.untouched.pk).exists())

    def test_merging_restores_the_displacement(self):
        """The whole point: before the merge the 2026 row has no predecessor."""
        before = PointsMovementRaw.objects.get(label="D01BIS")
        self.assertEqual(before.dt, 0)
        self.assertAlmostEqual(before.d, 0.0)

        self._run("--apply")

        after = PointsMovementRaw.objects.get(label="D01bis", survey_year=2026)
        self.assertEqual(after.dt, 370)
        self.assertAlmostEqual(after.d_e, 3.0, places=6)
        self.assertAlmostEqual(after.d_n, 4.0, places=6)

    def test_is_idempotent(self):
        self._run("--apply")
        self.assertIn("no duplicate labels", self._run("--apply"))


class CaseInsensitivePointImportTests(TestCase):
    """The importer must not create a second point differing only by case."""

    def test_existing_point_is_reused_regardless_of_case(self):
        from surveys.resources import MeasurementResource

        survey = Survey.objects.create(date=datetime.date(2026, 7, 25), year=2026)
        existing = Point.objects.create(label="D01bis", active=True, is_fixed=False)

        resource = MeasurementResource()
        resource.before_import(None)
        row = {
            "point_label": "D01BIS",
            "survey_id": str(survey.pk),
            "east": "416000",
            "north": "5090000",
            "h": "2100",
        }
        resource.before_import_row(row)

        self.assertEqual(row["point"], existing.pk)
        self.assertEqual(Point.objects.count(), 1)


class LabelUniquenessTests(TestCase):
    """The constraint that stops this recurring."""

    def test_case_variant_is_rejected(self):
        from django.db import IntegrityError, transaction

        Point.objects.create(label="D01bis", active=True, is_fixed=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Point.objects.create(label="D01BIS", active=True, is_fixed=False)

    def test_whitespace_variant_is_rejected(self):
        from django.db import IntegrityError, transaction

        Point.objects.create(label="D02bis", active=True, is_fixed=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Point.objects.create(label=" D02BIS ", active=True, is_fixed=False)

    def test_distinct_labels_are_still_allowed(self):
        Point.objects.create(label="D38", active=True, is_fixed=False)
        Point.objects.create(label="D38bis", active=True, is_fixed=False)
        self.assertEqual(Point.objects.filter(label__startswith="D38").count(), 2)


class PointDeletionTests(TestCase):
    """Deleting a point must not quietly take its measurements with it."""

    @classmethod
    def setUpTestData(cls):
        cls.survey = Survey.objects.create(date=datetime.date(2025, 7, 20), year=2025)
        cls.with_data = Point.objects.create(
            label="SMETEO", active=True, is_fixed=False
        )
        cls.empty = Point.objects.create(label="EMPTY1", active=True, is_fixed=False)
        Measurement.objects.create(
            point=cls.with_data,
            survey=cls.survey,
            east=416000.0,
            north=5090000.0,
            h=2100.0,
        )

    def test_point_with_measurements_is_protected(self):
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.with_data.delete()
        self.assertTrue(Point.objects.filter(pk=self.with_data.pk).exists())

    def test_point_without_measurements_deletes_normally(self):
        self.empty.delete()
        self.assertFalse(Point.objects.filter(pk=self.empty.pk).exists())

    def test_survey_with_measurements_is_protected(self):
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.survey.delete()

    def test_admin_action_deletes_point_and_its_measurements(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from surveys.admin import PointAdmin

        request = RequestFactory().post("/admin/surveys/point/")
        request.user = None
        # message_user needs a message store; the plain request has none
        request._messages = type("S", (), {"add": lambda *a, **k: None})()

        admin_instance = PointAdmin(Point, AdminSite())
        admin_instance.delete_with_measurements(
            request, Point.objects.filter(pk=self.with_data.pk)
        )

        self.assertFalse(Point.objects.filter(pk=self.with_data.pk).exists())
        self.assertEqual(
            Measurement.objects.filter(point_id=self.with_data.pk).count(), 0
        )


class MeasurementMapTests(TestCase):
    """The measurement changelist carries a GeoJSON map of the filtered rows."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import User

        cls.admin = User.objects.create_superuser("admin", "a@example.com", "pw")
        cls.s2024 = Survey.objects.create(date=datetime.date(2024, 7, 20), year=2024)
        cls.s2025 = Survey.objects.create(date=datetime.date(2025, 7, 20), year=2025)
        point = Point.objects.create(label="D12", active=True, is_fixed=False)
        # lat/lon are filled by the compute_measurement_geometry trigger.
        common = {"point": point, "east": 416125.449, "north": 5089423.209, "h": 2121.0}
        Measurement.objects.create(survey=cls.s2024, **common)
        Measurement.objects.create(survey=cls.s2025, **common)

    def test_geojson_is_wgs84_lon_lat(self):
        from surveys.admin import ChangeListMapMixin, changelist_geojson

        geojson = changelist_geojson(
            PointsMeasurement.objects.all(),
            ChangeListMapMixin.map_fields,
            ChangeListMapMixin.map_url_name,
            limit=10,
        )
        self.assertEqual(len(geojson["features"]), 2)
        feature = geojson["features"][0]
        self.assertEqual(feature["properties"]["label"], "D12")
        lon, lat = feature["geometry"]["coordinates"]
        # D12 sits at roughly 45.96 N, 7.93 E (GeoJSON order is lon, lat).
        self.assertAlmostEqual(lat, 45.96, delta=0.02)
        self.assertAlmostEqual(lon, 7.93, delta=0.02)

    def test_changelist_maps_follow_filters(self):
        self.client.force_login(self.admin)
        cases = (
            (
                "admin:surveys_measurement_changelist",
                "survey__id__exact",
                self.s2024.pk,
            ),
            ("admin:surveys_pointsmeasurement_changelist", "survey_year", 2024),
        )
        for url_name, filter_key, filter_value in cases:
            with self.subTest(url_name):
                url = reverse(url_name)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="changelist-map-geojson"')
                features = response.context["map_geojson"]["features"]
                self.assertEqual(len(features), 2)
                # Popups link to the editable measurement, also from the view.
                self.assertIn("/surveys/measurement/", features[0]["properties"]["url"])

                response = self.client.get(url, {filter_key: filter_value})
                self.assertEqual(len(response.context["map_geojson"]["features"]), 1)

        # The mixin's template must not drop the import-export buttons.
        response = self.client.get(reverse("admin:surveys_measurement_changelist"))
        self.assertContains(response, reverse("admin:surveys_measurement_import"))
