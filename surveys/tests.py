import datetime
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image as PILImage
from rest_framework.test import APITestCase

from surveys.models import (
    ActivePoint,
    Measurement,
    MeasurementPhoto,
    Point,
    PointMovement,
    PointsMeasurement,
    Survey,
)


class DatabaseViewTests(TestCase):
    """The migration-managed views must exist on a freshly migrated database
    and be queryable through their unmanaged models."""

    VIEW_MODELS = (
        PointsMeasurement,
        PointMovement,
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
            east=416100.0,
            north=5089400.0,
            h=2100.0,
        )
        cls.m2 = Measurement.objects.create(
            id=2,
            point=cls.point,
            survey=cls.survey_2016,
            east=416103.0,
            north=5089404.0,
            h=2100.0,
        )
        # short-gap measurement (dt = 100 days, must be excluded from velocity)
        cls.m3 = Measurement.objects.create(
            id=3,
            point=cls.point,
            survey=cls.survey_2016b,
            east=416104.0,
            north=5089405.0,
            h=2100.0,
        )
        cls.m_fixed = Measurement.objects.create(
            id=4,
            point=cls.fixed_point,
            survey=cls.survey_2015,
            east=416125.449,
            north=5089423.209,
            h=2121.172,
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
        self.assertEqual(record["east"], 416100.0)

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
        # 5 m on the UTM grid is ~0.06 % more on the ground (ENU, true distance).
        self.assertAlmostEqual(data[0]["v"], 5.0 / 365.0, delta=5.0 / 365.0 * 1e-3)

    def test_velocity_fixed_point_returns_empty(self):
        response = self.client.get(reverse("surveys:point-velocity", args=["FIX1"]))
        self.assertEqual(response.json(), [])

    def test_velocity_unknown_label_returns_empty(self):
        response = self.client.get(reverse("surveys:point-velocity", args=["NOPE"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


class PointMovementViewTests(TestCase):
    """The point_movements view: horizontal/3D values, flags and acceleration.

    Tolerances: true (ENU) distances are ~0.06 % longer than the UTM grid ones
    the fixtures are written in, and 365-day intervals are 0.07 % short of the
    365.25-day year the m/yr units use.
    """

    @classmethod
    def setUpTestData(cls):
        surveys = {
            key: Survey.objects.create(date=date, year=date.year)
            for key, date in {
                2020: datetime.date(2020, 7, 20),
                2021: datetime.date(2021, 7, 20),
                2022: datetime.date(2022, 7, 20),
                2023: datetime.date(2023, 7, 20),
                "2023b": datetime.date(2023, 10, 20),  # 92 days later
            }.items()
        }

        def series(
            label: str,
            east_by_survey: dict,
            *,
            fixed: bool = False,
            dh: float = 0.0,
            meas_dates: dict | None = None,
        ):
            point = Point.objects.create(label=label, is_fixed=fixed, active=True)
            for i, (key, east) in enumerate(east_by_survey.items()):
                Measurement.objects.create(
                    point=point,
                    survey=surveys[key],
                    east=416500.0 + east,
                    north=5089000.0,
                    h=2200.0 + dh * i,
                    meas_date=(meas_dates or {}).get(key),
                )

        # 10 m/yr east, 2 m/yr surface lowering, plus an intra-annual remeasure.
        series("S01", {2020: 0, 2021: 10, 2022: 20, 2023: 30, "2023b": 32}, dh=-2.0)
        # Constant acceleration 4 m/yr2: x = 2 t^2 -> yearly steps 2, 6, 10 m.
        series("S02", {2020: 0, 2021: 2, 2022: 8, 2023: 18})
        # Moves, but is a fixed mark: never appears.
        series("FIX", {2020: 0, 2021: 5}, fixed=True)
        # Measured 20 days after its campaign date.
        series(
            "S03",
            {2020: 0, 2021: 10},
            meas_dates={2021: datetime.date(2021, 8, 9)},
        )

    def rows(self, label: str) -> list[PointMovement]:
        return list(PointMovement.objects.filter(label=label).order_by("date_fin"))

    def test_only_pairs_of_moving_points(self):
        self.assertEqual(len(self.rows("S01")), 4)  # 5 measurements -> 4 pairs
        self.assertEqual(self.rows("FIX"), [])

    def test_horizontal_and_3d_values(self):
        first = self.rows("S01")[0]
        self.assertTrue(first.is_annual)
        self.assertAlmostEqual(first.d, 10.0, delta=0.01)
        self.assertAlmostEqual(first.d_3d, (10.0**2 + 2.0**2) ** 0.5, delta=0.01)
        self.assertAlmostEqual(first.d_h, -2.0, places=9)
        self.assertAlmostEqual(first.v, 10.0, delta=0.02)  # m/yr
        self.assertAlmostEqual(first.v_h, -2.0, delta=0.005)

    def test_acceleration(self):
        constant = self.rows("S01")
        self.assertIsNone(constant[0].a)  # no previous interval
        for row in constant[1:3]:
            self.assertAlmostEqual(row.a, 0.0, delta=1e-4)
        self.assertFalse(constant[3].is_annual)  # 92-day remeasure
        self.assertIsNone(constant[3].a)

        accelerating = self.rows("S02")
        self.assertIsNone(accelerating[0].a)
        for row in accelerating[1:]:
            self.assertAlmostEqual(row.a, 4.0, delta=0.02)

    def test_uses_measurement_date(self):
        (row,) = self.rows("S03")
        self.assertEqual(row.date_fin, datetime.date(2021, 8, 9))
        self.assertEqual(row.dt, 385)

    def test_api_serves_annual_horizontal_velocity_in_m_per_day(self):
        response = self.client.get(reverse("surveys:point-velocity", args=["S01"]))
        data = response.json()
        self.assertEqual(len(data), 3)  # the intra-annual pair is left out
        self.assertAlmostEqual(data[0]["v"], 10.0 / 365.0, delta=1e-4)
        self.assertEqual(
            self.client.get(reverse("surveys:point-velocity", args=["FIX"])).json(), []
        )

    def test_admin_page_and_map(self):
        from django.contrib.auth.models import User

        self.client.force_login(
            User.objects.create_superuser("admin", "a@example.com", "pw")
        )
        response = self.client.get(reverse("admin:surveys_pointmovement_changelist"))
        self.assertEqual(response.status_code, 200)
        features = response.context["map_geojson"]["features"]
        # All years by default, annual pairs only (S01 3, S02 3, S03 1): the
        # 92-day remeasure is not drawn.
        self.assertEqual(len(features), 3 + 3 + 1)
        # Values shown to 4 decimals; sorting still uses the full value.
        row = PointMovement.objects.filter(label="S01").order_by("date_fin").first()
        # (The map data keeps the full value; the table cell is rounded.)
        self.assertContains(response, f'<td class="field-v_rounded">{row.v:.4f}</td>')
        # Headers say which end of the movement the dates and year refer to.
        for header in ("Year (end)", "Start date", "End date"):
            self.assertContains(response, header)
        self.assertIn("end", response.context["field_help"]["survey_year"])
        self.assertEqual(features[0]["geometry"]["type"], "LineString")
        classes = response.context["map_classes"]
        self.assertEqual(classes["property"], "v")
        self.assertContains(response, 'id="changelist-map-classes"')
        # S01 moves 10 m/yr on the grid; true distance (+0.06 %) over 365 days of
        # a 365.25-day year puts it just above 10 -> "10–20" (upper bounds exclusive).
        s01 = next(f for f in features if f["properties"]["label"] == "S01")
        self.assertGreater(s01["properties"]["v"], 10.0)
        bucket = next(
            c
            for c in classes["classes"]
            if c["max"] is None or s01["properties"]["v"] < c["max"]
        )
        self.assertEqual(bucket["label"], "10–20")

        # Filters narrow the map (2021: S01, S02, S03).
        response = self.client.get(
            reverse("admin:surveys_pointmovement_changelist"), {"survey_year": "2021"}
        )
        self.assertEqual(len(response.context["map_geojson"]["features"]), 3)
        response = self.client.get(
            reverse("admin:surveys_pointmovement_changelist"), {"is_annual__exact": "0"}
        )
        self.assertEqual(response.context["cl"].result_count, 1)


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
        # Movements exist only for moving points; the fixture's original is a
        # fixed mark (other tests need that difference), so make it a stake here.
        Point.objects.filter(pk=self.original.pk).update(is_fixed=False)
        self.assertFalse(PointMovement.objects.filter(label="D01BIS").exists())

        self._run("--apply")

        after = PointMovement.objects.get(label="D01bis", survey_year=2026)
        self.assertEqual(after.dt, 370)
        # (3, 4) m on the UTM grid; d is the true horizontal distance (ENU).
        self.assertAlmostEqual(after.d, 5.0, delta=0.005)

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
            Measurement.objects.all(),
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

    def test_changelist_map_follows_filters(self):
        self.client.force_login(self.admin)
        url = reverse("admin:surveys_measurement_changelist")
        response = self.client.get(url)
        self.assertContains(response, 'id="changelist-map-geojson"')
        features = response.context["map_geojson"]["features"]
        self.assertEqual(len(features), 2)
        self.assertIn("/surveys/measurement/", features[0]["properties"]["url"])
        # The mixin's template must not drop the import-export buttons.
        self.assertContains(response, reverse("admin:surveys_measurement_import"))

        response = self.client.get(url, {"survey__year": 2024})
        self.assertEqual(len(response.context["map_geojson"]["features"]), 1)

    def test_changelist_shows_point_and_survey_columns(self):
        """Everything the removed points_measurements page listed is here."""
        self.client.force_login(self.admin)
        url = reverse("admin:surveys_measurement_changelist")
        response = self.client.get(url)
        for header in (
            "Label",
            "Survey year",
            "Survey date",
            "H orto",
            "Fixed",
            "Active",
        ):
            self.assertContains(response, header)
        self.assertContains(response, "July 20, 2024")  # survey_date column

        response = self.client.get(url, {"point__is_fixed__exact": "1"})
        self.assertEqual(response.context["cl"].result_count, 0)
        response = self.client.get(url, {"point__active__exact": "1"})
        self.assertEqual(response.context["cl"].result_count, 2)
        titles = [spec.title for spec in response.context["cl"].filter_specs]
        self.assertEqual(titles[:3], ["fixed point", "active point", "survey year"])

    def test_measurement_time_and_point_texts(self):
        self.client.force_login(self.admin)
        m = Measurement.objects.order_by("id").first()
        Measurement.objects.filter(pk=m.pk).update(
            meas_time=datetime.datetime(2026, 7, 27, 7, 11, 5, tzinfo=datetime.UTC),
            meas_strategy="RTK RADIO",
        )
        Point.objects.filter(pk=m.point_id).update(
            target="giallo e nero", description="sulla morena"
        )
        response = self.client.get(reverse("admin:surveys_measurement_changelist"))
        # Stored in UTC, shown in local (summer) time.
        self.assertContains(response, "2026-07-27 09:11:05 CEST")
        self.assertContains(response, "RTK RADIO")

        response = self.client.get(
            reverse("admin:surveys_measurement_change", args=[m.pk])
        )
        for text in (
            "Point (monograph)",
            "giallo e nero",
            "sulla morena",
            "Survey mode",
        ):
            self.assertContains(response, text)
        self.assertContains(
            response, reverse("admin:surveys_point_change", args=[m.point_id])
        )

    def test_view_admin_page_is_gone(self):
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get("/admin/surveys/pointsmeasurement/").status_code, 404
        )


class PointAdminTests(TestCase):
    """The Points page shows each point's latest position, list and map."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import User

        cls.admin = User.objects.create_superuser("admin", "a@example.com", "pw")
        s2024 = Survey.objects.create(date=datetime.date(2024, 7, 20), year=2024)
        s2025 = Survey.objects.create(date=datetime.date(2025, 7, 20), year=2025)
        cls.point = Point.objects.create(label="D12", active=True, is_fixed=False)
        Point.objects.create(label="NEW1", active=True, is_fixed=False)  # unmeasured
        # Entered out of order: "latest" must follow the survey date, not the id.
        for survey, east in ((s2025, 416010.0), (s2024, 416000.0)):
            Measurement.objects.create(
                point=cls.point, survey=survey, east=east, north=5089423.0, h=2121.0
            )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_changelist_shows_latest_position_and_map(self):
        response = self.client.get(reverse("admin:surveys_point_changelist"))
        self.assertEqual(response.status_code, 200)
        rows = {p.label: p for p in response.context["cl"].result_list}
        self.assertEqual(rows["D12"]._last_east, 416010.0)
        self.assertIsNone(rows["NEW1"]._last_east)
        for header in ("Last east", "Last north", "Last h"):
            self.assertContains(response, header)

        features = response.context["map_geojson"]["features"]
        self.assertEqual(len(features), 1)  # the unmeasured point has no marker
        props = features[0]["properties"]
        self.assertEqual((props["label"], props["year"]), ("D12", 2025))
        self.assertEqual(
            props["url"], reverse("admin:surveys_point_change", args=[self.point.pk])
        )

    def test_point_filters(self):
        url = reverse("admin:surveys_point_changelist")

        def labels(params: dict) -> set[str]:
            response = self.client.get(url, params)
            return {p.label for p in response.context["cl"].result_list}

        self.assertEqual(labels({"measurement__survey__year": 2024}), {"D12"})
        self.assertEqual(labels({"last_measured": "2025"}), {"D12"})
        self.assertEqual(labels({"last_measured": "2024"}), set())
        self.assertEqual(labels({"measurements": "0"}), {"NEW1"})
        self.assertEqual(labels({"measurements": "2-5"}), {"D12"})
        self.assertEqual(labels({"has_photos": "no"}), {"D12", "NEW1"})
        # Filtering on a year must not shrink the measurement count.
        response = self.client.get(url, {"measurement__survey__year": 2024})
        self.assertEqual(response.context["cl"].result_list[0]._num_measurements, 2)

    def test_hover_descriptions(self):
        from surveys.sql import COLUMN_COMMENTS

        response = self.client.get(reverse("admin:surveys_point_changelist"))
        help_texts = response.context["field_help"]
        self.assertEqual(help_texts["is_fixed"], COLUMN_COMMENTS["is_fixed"])
        self.assertIn("last_east", help_texts)
        self.assertContains(response, 'id="field-help"')

        response = self.client.get(
            reverse("admin:surveys_point_change", args=[self.point.pk])
        )
        self.assertContains(response, 'id="field-help"')

    def test_status_filter_defaults_to_active_points(self):
        Point.objects.create(label="OLD1", active=False, is_fixed=False)
        url = reverse("admin:surveys_point_changelist")

        def labels(params: dict) -> set[str]:
            response = self.client.get(url, params)
            return {p.label for p in response.context["cl"].result_list}

        response = self.client.get(url)
        self.assertNotIn("OLD1", {p.label for p in response.context["cl"].result_list})
        self.assertContains(response, "Showing active points only: 1 inactive")
        self.assertEqual(labels({"status": "inactive"}), {"OLD1"})
        self.assertIn("OLD1", labels({"status": "all"}))
        self.assertNotContains(
            self.client.get(url, {"status": "all"}), "Showing active"
        )
        # The point picker of the Measurement form lists every point.
        self.assertIn("OLD1", labels({"_popup": "1", "_to_field": "id"}))

    def test_active_points_view_page_is_gone(self):
        self.assertEqual(
            self.client.get("/admin/surveys/activepoint/").status_code, 404
        )


def _jpeg(name: str = "IMG_0001.JPG") -> SimpleUploadedFile:
    """A tiny valid JPEG upload."""
    buffer = BytesIO()
    PILImage.new("RGB", (8, 8), "red").save(buffer, format="JPEG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")


@override_settings(
    S3_ENDPOINT_URL="https://fake-s3.example",
    S3_ACCESS_KEY="key",
    S3_SECRET_KEY="secret",
    S3_READONLY_ACCESS_KEY="",
    S3_READONLY_SECRET_KEY="",
    S3_SURVEYS_BUCKET_NAME="bkt",
    S3_MEASUREMENT_PHOTOS_PREFIX="measurement_photos/",
)
class MeasurementPhotoTests(TestCase):
    """Photos are uploaded through the admin, kept in S3 and cleaned up with their row."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import User

        cls.admin = User.objects.create_superuser("admin", "a@example.com", "pw")
        survey = Survey.objects.create(date=datetime.date(2025, 7, 20), year=2025)
        point = Point.objects.create(label="D12", active=True, is_fixed=False)
        coords = {"east": 416125.449, "north": 5089423.209, "h": 2121.0}
        cls.measurement = Measurement.objects.create(
            point=point, survey=survey, **coords
        )
        cls.other = Measurement.objects.create(point=point, survey=survey, **coords)

    def setUp(self):
        from django.core.files.storage import InMemoryStorage

        field = MeasurementPhoto._meta.get_field("image")
        original, field.storage = field.storage, InMemoryStorage()
        self.addCleanup(setattr, field, "storage", original)
        self.storage = field.storage
        self.client.force_login(self.admin)

    def _photo(self, measurement=None) -> MeasurementPhoto:
        return MeasurementPhoto.objects.create(
            measurement=measurement or self.measurement, image=_jpeg()
        )

    def test_admin_upload_stores_file_under_measurement_prefix(self):
        response = self.client.post(
            reverse("admin:surveys_measurementphoto_add"),
            {"measurement": self.measurement.pk, "image": _jpeg("IMG_0042.JPG")},
        )
        self.assertEqual(response.status_code, 302)
        photo = MeasurementPhoto.objects.get()
        self.assertEqual(photo.file_name, "IMG_0042.JPG")
        self.assertIsNotNone(photo.uploaded_at)
        self.assertRegex(
            photo.image.name,
            rf"^measurement_photos/{self.measurement.pk}/[0-9a-f]{{32}}\.jpg$",
        )
        self.assertTrue(self.storage.exists(photo.image.name))

    def test_admin_rejects_non_image(self):
        response = self.client.post(
            reverse("admin:surveys_measurementphoto_add"),
            {
                "measurement": self.measurement.pk,
                "image": SimpleUploadedFile("notes.jpg", b"not an image"),
            },
        )
        self.assertEqual(response.status_code, 200)  # form redisplayed with error
        self.assertFalse(MeasurementPhoto.objects.exists())

    def test_measurement_can_have_several_photos(self):
        self._photo()
        self._photo()
        self.assertEqual(self.measurement.photos.count(), 2)

    def test_replacing_deletes_old_file(self):
        photo = self._photo()
        old_name = photo.image.name
        with self.captureOnCommitCallbacks(execute=True):
            photo.image = _jpeg("IMG_0002.JPG")
            photo.save()
        self.assertNotEqual(photo.image.name, old_name)
        self.assertEqual(photo.file_name, "IMG_0002.JPG")
        self.assertFalse(self.storage.exists(old_name))
        self.assertTrue(self.storage.exists(photo.image.name))

    def test_deleting_photo_or_measurement_deletes_files(self):
        kept = self._photo()
        photo = self._photo(self.other)
        cascaded = self._photo(self.other)
        with self.captureOnCommitCallbacks(execute=True):
            photo.delete()
        self.assertFalse(self.storage.exists(photo.image.name))
        with self.captureOnCommitCallbacks(execute=True):
            self.other.delete()
        self.assertFalse(self.storage.exists(cascaded.image.name))
        self.assertTrue(self.storage.exists(kept.image.name))

    def test_photo_url_redirects_staff_to_presigned_link(self):
        photo = self._photo()
        url = reverse("surveys:measurement-photo", args=[photo.pk])
        self.assertEqual(photo.get_absolute_url(), url)

        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(photo.image.name, response["Location"])
        self.assertIn("X-Amz-Signature=", response["Location"])

        self.assertEqual(
            self.client.get(
                reverse("surveys:measurement-photo", args=[999])
            ).status_code,
            404,
        )
        self.client.logout()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_measurement_admin_shows_photos(self):
        self._photo()
        changelist = reverse("admin:surveys_measurement_changelist")
        response = self.client.get(changelist, {"has_photos": "yes"})
        self.assertEqual(response.context["cl"].result_count, 1)
        response = self.client.get(changelist, {"has_photos": "no"})
        self.assertEqual(response.context["cl"].result_count, 1)

        response = self.client.get(
            reverse("admin:surveys_measurement_change", args=[self.measurement.pk])
        )
        self.assertContains(response, 'name="photos-TOTAL_FORMS"')
        self.assertContains(response, "IMG_0001.JPG")


def _photo_bytes(
    size: tuple[int, int] = (40, 20), orientation: int | None = None
) -> bytes:
    """A JPEG with a red top-left corner, optionally EXIF-tagged."""
    image = PILImage.new("RGB", size, "white")
    image.paste((255, 0, 0), (0, 0, 8, 8))
    exif = PILImage.Exif()
    if orientation:
        exif[0x0112] = orientation
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=95, exif=exif.tobytes())
    return buffer.getvalue()


class PhotoUtilsTests(TestCase):
    def test_parse_photo_name_reads_local_time(self):
        from pathlib import Path
        from zoneinfo import ZoneInfo

        from surveys.utils.photos import parse_photo_name

        photo = parse_photo_name(
            Path("Img_D01BIS_270726_091105.jpg"), ZoneInfo("Europe/Rome")
        )
        self.assertEqual(photo.label, "D01BIS")
        # 09:11:05 CEST (UTC+2) is 07:11:05 UTC.
        self.assertEqual(
            photo.taken_at.astimezone(datetime.UTC),
            datetime.datetime(2026, 7, 27, 7, 11, 5, tzinfo=datetime.UTC),
        )
        self.assertIsNone(parse_photo_name(Path("notes.txt"), ZoneInfo("UTC")))

    def test_upright_jpeg_applies_exif_then_extra_rotation(self):
        from surveys.utils.photos import exif_orientation, upright_jpeg

        def opened(data: bytes) -> PILImage.Image:
            return PILImage.open(BytesIO(data))

        # Tag 6 = "rotate 90 clockwise to display": pixels come out portrait.
        tagged = upright_jpeg(_photo_bytes(orientation=6))
        self.assertEqual(opened(tagged).size, (20, 40))
        self.assertIsNone(exif_orientation(tagged))  # nothing rotates it again

        # Untagged + 90 counterclockwise: the red top-left corner ends bottom-left.
        turned = opened(upright_jpeg(_photo_bytes(), extra_rotation=90))
        self.assertEqual(turned.size, (20, 40))
        red, green, _ = turned.getpixel((2, 37))
        self.assertGreater(red, 200)
        self.assertLess(green, 80)


@override_settings(
    S3_ENDPOINT_URL="https://fake-s3.example",
    S3_ACCESS_KEY="key",
    S3_SECRET_KEY="secret",
    S3_SURVEYS_BUCKET_NAME="bkt",
    S3_MEASUREMENT_PHOTOS_PREFIX="measurement_photos/",
)
class ImportPointPhotosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        survey = Survey.objects.create(date=datetime.date(2026, 7, 26), year=2026)
        coords = {"east": 416500.0, "north": 5089000.0, "h": 2200.0}
        cls.m = {
            label: Measurement.objects.create(
                point=Point.objects.create(label=label, is_fixed=False, active=True),
                survey=survey,
                **coords,
            )
            for label in ("S01", "S02", "S03", "S04")
        }
        # Already has a time: kept unless --overwrite-times.
        cls.m["S03"].meas_time = datetime.datetime(
            2026, 7, 26, 6, 0, tzinfo=datetime.UTC
        )
        cls.m["S03"].save()

    def setUp(self):
        import tempfile
        from pathlib import Path

        from django.core.files.storage import InMemoryStorage

        field = MeasurementPhoto._meta.get_field("image")
        original, field.storage = field.storage, InMemoryStorage()
        self.addCleanup(setattr, field, "storage", original)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        files = {
            "Img_S01_270726_091500.jpg": _photo_bytes(),
            "Img_S01_270726_091105.jpg": _photo_bytes(),  # earlier: gives the time
            "Img_SS02_280726_100000.jpg": _photo_bytes(orientation=8),  # misspelt
            "Img_S03_290726_120000.jpg": _photo_bytes(),
            "Img_NOPE_270726_080000.jpg": _photo_bytes(),  # no such point
            "notes.txt": b"not a photo",
        }
        for name, data in files.items():
            (self.dir / name).write_bytes(data)
        (self.dir / "rotations.csv").write_text(
            "# comment\nImg_S01_270726_091105.jpg, 90\n"
        )

    def run_command(self, *extra: str) -> str:
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command(
            "import_point_photos", str(self.dir), "--year", "2026",
            "--alias", "SS02=S02", *extra, stdout=out,
        )  # fmt: skip
        return out.getvalue()

    def refreshed(self, label: str) -> Measurement:
        return Measurement.objects.get(pk=self.m[label].pk)

    def test_dry_run_changes_nothing(self):
        output = self.run_command()
        self.assertIn("dry run", output)
        self.assertIn("Img_NOPE_270726_080000.jpg", output)  # reported unmatched
        self.assertIn("S04", output)  # reported without a photo
        self.assertFalse(MeasurementPhoto.objects.exists())
        self.assertIsNone(self.refreshed("S01").meas_time)
        self.assertTrue((self.dir / "_contact_sheet.jpg").exists())

    def test_apply_uploads_upright_photos_and_sets_times(self):
        self.run_command("--apply")
        s01 = self.refreshed("S01")
        self.assertEqual(
            s01.meas_time, datetime.datetime(2026, 7, 27, 7, 11, 5, tzinfo=datetime.UTC)
        )
        self.assertEqual(s01.meas_date, datetime.date(2026, 7, 27))
        self.assertEqual(s01.photos.count(), 2)
        rotated = s01.photos.get(file_name="Img_S01_270726_091105.jpg")
        self.assertEqual(PILImage.open(rotated.image.open()).size, (20, 40))
        # Alias resolved; EXIF tag 8 applied.
        s02_photo = self.refreshed("S02").photos.get()
        self.assertEqual(PILImage.open(s02_photo.image.open()).size, (20, 40))
        # Existing time kept; the date was empty, so it is filled.
        s03 = self.refreshed("S03")
        self.assertEqual(s03.meas_time.hour, 6)
        self.assertEqual(s03.meas_date, datetime.date(2026, 7, 29))

        # Re-running uploads nothing new.
        self.assertIn("0 photo(s) uploaded", self.run_command("--apply"))
        self.assertEqual(MeasurementPhoto.objects.count(), 4)

    def test_replace_attached_reuploads_upright(self):
        from django.core.files.base import ContentFile

        # An original (sideways, untagged) uploaded earlier through the admin.
        old = MeasurementPhoto.objects.create(
            measurement=self.m["S01"],
            image=ContentFile(_photo_bytes(), name="Img_S01_270726_091105.jpg"),
        )
        old_key = old.image.name
        storage = old.image.storage
        # Without --replace-attached the original is kept as it is.
        self.assertIn("1 already attached (kept)", self.run_command("--apply"))
        self.assertEqual(PILImage.open(storage.open(old_key)).size, (40, 20))  # skipped

        with self.captureOnCommitCallbacks(execute=True):
            self.run_command("--apply", "--replace-attached")
        old.refresh_from_db()
        self.assertNotEqual(old.image.name, old_key)
        self.assertEqual(PILImage.open(old.image.open()).size, (20, 40))
        self.assertFalse(storage.exists(old_key))  # inside the prefix: deleted

    def test_never_deletes_files_outside_this_prefix(self):
        from django.core.files.base import ContentFile

        photo = MeasurementPhoto.objects.create(
            measurement=self.m["S04"], image=ContentFile(_photo_bytes(), name="a.jpg")
        )
        storage = photo.image.storage
        # A production key, as seen from a local copy that uploads under dev/.
        storage.save("measurement_photos/1/prod.jpg", ContentFile(b"x"))
        MeasurementPhoto.objects.filter(pk=photo.pk).update(
            image="measurement_photos/1/prod.jpg"
        )
        photo.refresh_from_db()
        with self.settings(S3_MEASUREMENT_PHOTOS_PREFIX="dev/measurement_photos/"):
            with self.captureOnCommitCallbacks(execute=True):
                photo.delete()
        self.assertTrue(storage.exists("measurement_photos/1/prod.jpg"))

    def test_overwrite_times(self):
        self.run_command("--apply", "--overwrite-times")
        self.assertEqual(
            self.refreshed("S03").meas_time,
            datetime.datetime(2026, 7, 29, 10, 0, tzinfo=datetime.UTC),
        )


def _odg(path, pages: list[list[str]]) -> None:
    """A minimal .odg with one draw:page per list of paragraphs."""
    import zipfile
    from xml.sax.saxutils import escape

    body = "".join(
        "<draw:page>"
        + "".join(f"<text:p>{escape(p)}</text:p>" for p in page)
        + "</draw:page>"
        for page in pages
    )
    content = (
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        f"<office:body><office:drawing>{body}</office:drawing></office:body>"
        "</office:document-content>"
    )
    with zipfile.ZipFile(path, "w") as odg:
        odg.writestr("content.xml", content)


HEADER = ["Politecnico di Milano", "Dipartimento di Ingegneria Civile e Ambientale"]


class PublisherParserTests(TestCase):
    def test_fields_and_quirks(self):
        from surveys.utils.publisher import parse_page

        page = parse_page(
            ["D38/D38BIS", *HEADER,
             "Target: giallo e nero, sostituito nel 2026Descrizione: su un masso",
             "E’ un punto FISSO",
             # Decomposed "à" and a non-breaking space, as on some real pages.
             "Modalità di rilievo GNSS: RTK-RADIO",
             "NOTA: target inclinato",
             "Non misurato nel 2023",
             "Coordinate 27-07-2024", "X [m]", "4399847.184"]
        )  # fmt: skip
        self.assertEqual(page.labels, ["D38", "D38BIS"])
        self.assertEqual(page.target, "giallo e nero, sostituito nel 2026")
        self.assertEqual(page.description, "su un masso")
        self.assertEqual(page.survey_mode, "RTK-RADIO")
        self.assertEqual(page.notes, "target inclinato Non misurato nel 2023")
        self.assertTrue(page.fixed)

    def test_inline_remarks(self):
        from surveys.utils.publisher import parse_page

        page = parse_page(
            ["D06", *HEADER,
             "Target: bianco e nero",
             "Descrizione: sul lago. E’ un punto FISSO",
             "E’ un punto FISSO instabile (perdita 7 cm)",
             "Descrizione2 non esiste. Modalità di rilievo GNSS: RTK"]
        )  # fmt: skip
        self.assertEqual(page.description, "sul lago.")  # remark taken out
        self.assertTrue(page.fixed)
        self.assertEqual(
            page.notes,
            "E’ un punto FISSO instabile (perdita 7 cm) Descrizione2 non esiste.",
        )
        self.assertEqual(page.survey_mode, "RTK")

    def test_not_a_point_page(self):
        from surveys.utils.publisher import parse_page

        self.assertIsNone(
            parse_page(["Monografie delle mire ottiche", "Redatto nel 2026"])
        )

    def test_split_survey_mode(self):
        from surveys.utils.publisher import split_survey_mode

        self.assertEqual(split_survey_mode("RTK-RADIO", 2026), ("RTK-RADIO", ""))
        history = "RTK dal 2023, STATICO in precedenza"
        self.assertEqual(split_survey_mode(history, 2026), ("RTK", history))
        tagged = "RTK RADIO, nRTK (2024)"
        self.assertEqual(split_survey_mode(tagged, 2024), ("nRTK", tagged))
        self.assertEqual(split_survey_mode(tagged, 2026), ("RTK RADIO", tagged))

    def test_split_labelled_notes(self):
        from surveys.utils.publisher import split_labelled_notes

        self.assertEqual(
            split_labelled_notes("Target: chiodo rosso. Descrizione: vicino al D05."),
            {"notes": "", "target": "chiodo rosso.", "description": "vicino al D05."},
        )
        self.assertEqual(split_labelled_notes("punto perso"), {})


class ImportMonographTextsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        def point(label: str, **fields) -> Point:
            return Point.objects.create(
                label=label, is_fixed=False, active=True, **fields
            )

        cls.d01 = point("D01bis")
        cls.d02 = point("D02", target="già scritto")
        coords = {"east": 416500.0, "north": 5089000.0, "h": 2200.0}
        s2025 = Survey.objects.create(date=datetime.date(2025, 7, 21), year=2025)
        s2026 = Survey.objects.create(date=datetime.date(2026, 7, 26), year=2026)
        Measurement.objects.create(point=cls.d01, survey=s2025, **coords)
        cls.d01_2026 = Measurement.objects.create(point=cls.d01, survey=s2026, **coords)
        # Explicitly marked: must stay.
        cls.d02_2026 = Measurement.objects.create(
            point=cls.d02, survey=s2026, meas_strategy="RTK SPIN3", **coords
        )
        cls.d63 = point(
            "D63", notes="Automatically created during measurement CSV import"
        )
        cls.foto = point(
            "FOTO 3", notes="Target: Lago locce. Descrizione: vicino al D05."
        )
        cls.other = point("D99", notes="punto perso")

    def setUp(self):
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.odg = Path(tmp.name) / "monografie.odg"
        _odg(
            self.odg,
            [
                ["Monografie delle mire ottiche"],
                ["D01BIS", *HEADER, "Target: giallo e nero", "Descrizione: sulla morena",
                 "Modalità di rilievo GNSS: RTK RADIO dal 2023, STATICO in precedenza",
                 "NOTA: target da sostituire"],
                ["D02", *HEADER, "Target: nuovo testo", "Descrizione: altrove",
                 "Modalità di rilievo GNSS: RTK-RADIO"],
                ["D63", *HEADER, "Target: giallo e blu."],
                ["D77", *HEADER, "Target: punto sconosciuto"],
            ],
        )  # fmt: skip

    def run_command(self, *extra: str) -> str:
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("import_monograph_texts", str(self.odg), *extra, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        output = self.run_command()
        self.assertIn("dry run", output)
        self.assertIn("D77", output)  # page with no such point
        self.d01.refresh_from_db()
        self.assertEqual(self.d01.target, "")

    def test_apply(self):
        output = self.run_command("--apply")
        self.d01.refresh_from_db()
        self.assertEqual(
            (self.d01.target, self.d01.description), ("giallo e nero", "sulla morena")
        )
        # Current mode on the latest measurement, history in the point notes.
        self.d01_2026.refresh_from_db()
        self.assertEqual(self.d01_2026.meas_strategy, "RTK RADIO")
        self.assertEqual(
            self.d01.notes,
            "target da sostituire Survey mode history: "
            "RTK RADIO dal 2023, STATICO in precedenza",
        )
        # An explicit survey mode is kept.
        self.d02_2026.refresh_from_db()
        self.assertEqual(self.d02_2026.meas_strategy, "RTK SPIN3")
        # Existing text kept, the empty field filled.
        self.d02.refresh_from_db()
        self.assertEqual(
            (self.d02.target, self.d02.description), ("già scritto", "altrove")
        )
        self.assertIn("kept existing text", output)
        # The CSV import's placeholder note is cleared.
        self.d63.refresh_from_db()
        self.assertEqual((self.d63.target, self.d63.notes), ("giallo e blu.", ""))
        # Labelled old notes split into the fields; other notes untouched.
        self.foto.refresh_from_db()
        self.assertEqual(
            (self.foto.target, self.foto.description, self.foto.notes),
            ("Lago locce.", "vicino al D05.", ""),
        )
        self.other.refresh_from_db()
        self.assertEqual(self.other.notes, "punto perso")

        # Re-running adds nothing (notes are not appended twice).
        self.assertIn("0 point(s) to update", self.run_command("--apply"))

    def test_overwrite(self):
        self.run_command("--apply", "--overwrite")
        self.d02.refresh_from_db()
        self.assertEqual(self.d02.target, "nuovo testo")


class MonographTests(TestCase):
    """Monograph data, formatting and the PDF command (no network, no S3)."""

    @classmethod
    def setUpTestData(cls):
        s2025 = Survey.objects.create(date=datetime.date(2025, 7, 21), year=2025)
        s2026 = Survey.objects.create(date=datetime.date(2026, 7, 26), year=2026)
        cls.point = Point.objects.create(
            label="D01bis", is_fixed=False, active=True, target="giallo e nero"
        )
        Point.objects.create(label="D99", is_fixed=True, active=True)  # never measured
        coords = {"east": 415924.8624, "north": 5089146.055, "h": 2140.4858}
        cls.m2025 = Measurement.objects.create(point=cls.point, survey=s2025, **coords)
        cls.m2026 = Measurement.objects.create(
            point=cls.point,
            survey=s2026,
            meas_strategy="RTK RADIO",
            meas_time=datetime.datetime(2026, 7, 27, 7, 11, 5, tzinfo=datetime.UTC),
            **coords,
        )

    def setUp(self):
        import tempfile
        from pathlib import Path

        from django.core.files.base import ContentFile
        from django.core.files.storage import InMemoryStorage

        field = MeasurementPhoto._meta.get_field("image")
        original, field.storage = field.storage, InMemoryStorage()
        self.addCleanup(setattr, field, "storage", original)
        MeasurementPhoto.objects.create(
            measurement=self.m2026,
            image=ContentFile(_photo_bytes(orientation=6), name="a.jpg"),
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_geocentric_matches_2024_monograph(self):
        from surveys.monographs import geocentric

        # D01bis 2024 in Monografie_dopoSS2024.pdf: same E, N, h as the database.
        x, y, z = geocentric(415924.8624, 5089146.055, 2140.4858)
        expected_xyz = (4401410.1987, 611934.0827, 4562980.5427)
        for got, expected in zip((x, y, z), expected_xyz, strict=True):
            self.assertAlmostEqual(got, expected, delta=0.001)

    def test_dms(self):
        from surveys.monographs import dms

        self.assertEqual(
            dms(45 + 57 / 60 + 2.6844 / 3600, "N", "S"), "45° 57′ 02.6844″ N"
        )
        # 59.99999″ rounds up into the minutes, never "60″".
        self.assertEqual(
            dms(7 + 54 / 60 + 59.99999 / 3600, "E", "W"), "7° 55′ 00.0000″ E"
        )

    def test_wgs84_block_has_dms_and_decimal(self):
        from surveys.monographs import coordinate_blocks

        # lat/lon are filled by the database trigger: read the stored row.
        rows = coordinate_blocks(Measurement.objects.get(pk=self.m2026.pk))[1]["rows"]
        values = [value for _, value, _ in rows]
        self.assertRegex(values[0], r"^45° 57′ \d\d\.\d{4}″ N$")
        self.assertRegex(values[1], r"^45\.\d{9}°$")
        self.assertRegex(values[2], r"^7° 54′ \d\d\.\d{4}″ E$")
        self.assertRegex(values[3], r"^7\.\d{9}°$")

    def test_collect_uses_data_up_to_the_year(self):
        from surveys.monographs import collect

        latest = collect(self.point, None, self.dir)
        self.assertEqual(latest.measurement, self.m2026)
        self.assertEqual(latest.measured_at, "2026-07-27 09:11 CEST")
        self.assertEqual(len(latest.photos), 1)
        # Stored with EXIF tag 6 (40x20): embedded upright.
        self.assertEqual(PILImage.open(latest.files[0]).size, (20, 40))

        as_of_2025 = collect(self.point, 2025, self.dir)
        self.assertEqual(as_of_2025.measurement, self.m2025)
        self.assertEqual(as_of_2025.photos, [])  # the 2026 photo is not used
        self.assertIsNone(collect(Point.objects.get(label="D99"), None, self.dir))

    def test_basemap_position_and_scale(self):
        from surveys.utils.basemap import Basemap, to_mercator

        xmin, ymax = to_mercator(7.90, 45.98)
        xmax, ymin = to_mercator(7.94, 45.94)
        basemap = Basemap(self.dir / "m.jpg", (xmin, ymin, xmax, ymax), 1000, 750, 3.0)
        self.assertEqual(basemap.position(7.90, 45.98), (0.0, 0.0))
        x, y = basemap.position(7.94, 45.94)
        self.assertAlmostEqual(x, 100.0)
        self.assertAlmostEqual(y, 100.0)
        length, percent = basemap.scale_bar()  # 20 % of 3000 m -> 500 m
        self.assertEqual((length, round(percent, 3)), (500, 16.667))

    def test_command_writes_pdfs(self):
        from io import StringIO
        from unittest.mock import patch

        from django.core.management import call_command

        from surveys.utils.basemap import Basemap

        def fake_basemap(lonlats, out, cache_dir):
            PILImage.new("RGB", (400, 300), "grey").save(out)
            return Basemap(
                out, (877000.0, 5772000.0, 884000.0, 5777250.0), 400, 300, 17.5
            )

        out = StringIO()
        with patch(
            "surveys.monographs.build_basemap",
            fake_basemap,
        ):
            call_command(
                "generate_monographs", "--output", str(self.dir), "--split", stdout=out
            )
        output = out.getvalue()
        self.assertIn("skipped: D99", output)
        pdfs = sorted(self.dir.glob("monographs_latest_*.pdf"))
        self.assertEqual(len(pdfs), 1)
        self.assertTrue(pdfs[0].read_bytes().startswith(b"%PDF"))
        self.assertIn("(2 pages)", output)  # cover + one point
        self.assertTrue((self.dir / "points" / "D01bis.pdf").exists())


# The manifest storage of production only knows files after collectstatic.
@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)
class MonographAdminTests(MonographTests):
    """The monograph buttons in the Points admin (same fixtures, fake imagery)."""

    def setUp(self):
        from unittest.mock import patch

        from django.contrib.auth.models import User

        from surveys.utils.basemap import Basemap

        super().setUp()

        def fake_basemap(lonlats, out, cache_dir):
            PILImage.new("RGB", (400, 300), "grey").save(out)
            return Basemap(
                out, (877000.0, 5772000.0, 884000.0, 5777250.0), 400, 300, 17.5
            )

        patcher = patch("surveys.monographs.build_basemap", fake_basemap)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(
            User.objects.create_superuser("admin", "a@example.com", "pw")
        )

    def url(self, name: str, *args) -> str:
        return reverse(f"admin:surveys_point_{name}", args=args)

    def test_buttons_on_list_and_form(self):
        response = self.client.get(self.url("changelist"))
        self.assertContains(response, self.url("monographs"))
        self.assertContains(response, self.url("monograph_pdf", self.point.pk))
        response = self.client.get(self.url("change", self.point.pk))
        self.assertContains(response, "View monograph")
        self.assertContains(response, self.url("monograph_pdf", self.point.pk))

    def test_view_one_point_in_the_browser(self):
        response = self.client.get(self.url("monograph", self.point.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "D01bis")
        # Browser version: stable photo URL, static logo, link to the PDF.
        self.assertContains(response, MeasurementPhoto.objects.get().get_absolute_url())
        self.assertContains(response, "/static/surveys/monograph/polimi.jpg")
        self.assertContains(response, "Download PDF")

    def test_export_pdfs(self):
        response = self.client.get(self.url("monograph_pdf", self.point.pk))
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn("monograph_D01bis_", response["Content-Disposition"])

        # The list's filters apply to "all points".
        response = self.client.get(self.url("monographs_pdf"), {"status": "active"})
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn("monographs_", response["Content-Disposition"])

    def test_export_latest_measurements_csv(self):
        import csv as csvlib
        import io

        Point.objects.create(label="OLD1", active=False, is_fixed=False)
        response = self.client.get(self.url("export_latest"))
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("points_latest_measurement_", response["Content-Disposition"])
        text = response.content.decode("utf-8-sig")
        rows = {r["label"]: r for r in csvlib.DictReader(io.StringIO(text))}
        # Active points only, as the list shows by default.
        self.assertEqual(set(rows), {"D01bis", "D99"})
        d01 = rows["D01bis"]
        self.assertEqual(d01["survey_year"], "2026")  # the latest measurement
        self.assertEqual(d01["meas_time_local"], "2026-07-27 09:11:05 CEST")
        self.assertEqual(d01["survey_mode"], "RTK RADIO")
        self.assertEqual(d01["east_7791"], "415924.8624")
        self.assertAlmostEqual(float(d01["x_6704"]), 4401410.1983, delta=0.001)
        self.assertEqual(d01["target"], "giallo e nero")
        self.assertEqual(d01["photos"], "1")
        self.assertEqual(rows["D99"]["survey_year"], "")  # never measured
        # Filters apply: all points.
        text = self.client.get(self.url("export_latest"), {"status": "all"}).content
        self.assertIn(b"OLD1", text)
        # The button is on the list.
        self.assertContains(
            self.client.get(self.url("changelist")), self.url("export_latest")
        )

    def test_point_form_shows_latest_photos(self):
        photo = MeasurementPhoto.objects.get()
        response = self.client.get(self.url("change", self.point.pk))
        self.assertContains(response, "Latest photos")
        self.assertContains(response, photo.get_absolute_url())
        self.assertContains(response, "Measured 2026-07-26 (campaign 2026)")
        self.assertContains(response, "All 1 photo(s) of D01bis")
        response = self.client.get(
            self.url("change", Point.objects.get(label="D99").pk)
        )
        self.assertContains(response, "No photos yet.")

    def test_point_form_shows_latest_coordinates(self):
        response = self.client.get(self.url("change", self.point.pk))
        self.assertContains(response, "Latest data")  # its own section
        self.assertContains(response, "Latest coordinates")
        self.assertContains(response, "Measured 2026-07-27 09:11 CEST")
        self.assertContains(response, "RDN2008 / UTM zone 32N")
        self.assertContains(response, "415924.862 m")
        self.assertContains(
            response, reverse("admin:surveys_measurement_change", args=[self.m2026.pk])
        )
        response = self.client.get(
            self.url("change", Point.objects.get(label="D99").pk)
        )
        self.assertContains(response, "Not measured yet.")

    def test_requires_staff(self):
        self.client.logout()
        response = self.client.get(self.url("monograph", self.point.pk))
        self.assertEqual(response.status_code, 302)


class AdminGuideTests(TestCase):
    def test_guide_page_and_header_link(self):
        from django.contrib.auth.models import User

        url = reverse("admin-guide")
        self.assertEqual(self.client.get(url).status_code, 302)  # staff only
        self.client.force_login(
            User.objects.create_superuser("admin", "a@example.com", "pw")
        )
        response = self.client.get(url)
        self.assertContains(response, "Data model")
        self.assertContains(response, "ITALGEO05")
        # Linked from the header of every admin page.
        self.assertContains(self.client.get(reverse("admin:index")), f'href="{url}"')
