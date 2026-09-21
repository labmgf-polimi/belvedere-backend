from datetime import timedelta
from io import BytesIO
from unittest.mock import Mock, patch

from django.core.management import CommandError, call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils.timezone import now as tz_now
from PIL import Image as PILImage
from PIL import ImageDraw
from rest_framework.test import APITestCase

from image_index.image_metadata import parse_datetime_from_filename
from image_index.models import Camera, Image
from image_index.views import serve_image, serve_image_preview, serve_image_thumbnail


class ImageServeViewTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.camera = Camera.objects.create(
            camera_name="Test camera",
            slug="test-camera",
            s3_bucket="belvedere-images",
            s3_prefix="test/",
        )
        self.image_bytes = self._make_test_image_bytes((2000, 1200))
        self.image = Image.objects.create(
            camera=self.camera,
            bucket="belvedere-images",
            object_key="test/test-image.png",
            filename="test-image.png",
            mime_type="image/png",
        )

    def _make_test_image_bytes(self, size):
        image = PILImage.new("RGB", size)
        draw = ImageDraw.Draw(image)
        for y in range(size[1]):
            color = (y % 256, (y * 3) % 256, (y * 7) % 256)
            draw.line((0, y, size[0], y), fill=color)

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _patch_s3_access(self):
        client = Mock()
        return patch("image_index.views.build_s3_client", return_value=client), patch(
            "image_index.views.get_object_bytes",
            return_value=(self.image_bytes, "image/png"),
        )

    def test_serve_image_returns_original_bytes(self):
        request = self.factory.get("/images/1/serve/")
        build_client_patch, get_bytes_patch = self._patch_s3_access()

        with build_client_patch, get_bytes_patch:
            response = serve_image(request, self.image.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.image_bytes)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_preview_and_thumbnail_are_resized(self):
        request = self.factory.get("/images/1/preview/")
        build_client_patch, get_bytes_patch = self._patch_s3_access()

        with build_client_patch, get_bytes_patch:
            preview_response = serve_image_preview(request, self.image.pk)
            thumbnail_response = serve_image_thumbnail(request, self.image.pk)

        preview_image = PILImage.open(BytesIO(preview_response.content))
        thumbnail_image = PILImage.open(BytesIO(thumbnail_response.content))

        self.assertLessEqual(preview_image.width, 1280)
        self.assertLessEqual(preview_image.height, 960)
        self.assertLessEqual(thumbnail_image.width, 160)
        self.assertLessEqual(thumbnail_image.height, 120)
        self.assertLess(len(preview_response.content), len(self.image_bytes))
        self.assertLess(len(thumbnail_response.content), len(self.image_bytes))


class CameraListAPITests(APITestCase):
    def setUp(self):
        self.active = Camera.objects.create(
            camera_name="Active Cam",
            slug="active-cam",
            s3_bucket="b",
            s3_prefix="p/",
            is_active=True,
        )
        self.inactive = Camera.objects.create(
            camera_name="Inactive Cam",
            slug="inactive-cam",
            s3_bucket="b",
            s3_prefix="q/",
            is_active=False,
        )

    def test_lists_all_cameras_by_default(self):
        response = self.client.get(reverse("api-cameras"))
        self.assertEqual(response.status_code, 200)
        slugs = [c["slug"] for c in response.data]
        self.assertIn("active-cam", slugs)
        self.assertIn("inactive-cam", slugs)

    def test_active_filter(self):
        response = self.client.get(reverse("api-cameras"), {"active": "true"})
        slugs = [c["slug"] for c in response.data]
        self.assertIn("active-cam", slugs)
        self.assertNotIn("inactive-cam", slugs)

    def test_response_fields(self):
        response = self.client.get(reverse("api-cameras"))
        camera = next(c for c in response.data)
        self.assertEqual(
            set(camera.keys()),
            {"id", "slug", "camera_name", "installation_date", "is_active"},
        )


class ImageListAPITests(APITestCase):
    def setUp(self):
        self.camera = Camera.objects.create(
            camera_name="Cam A", slug="cam-a", s3_bucket="b", s3_prefix="a/"
        )
        self.other = Camera.objects.create(
            camera_name="Cam B", slug="cam-b", s3_bucket="b", s3_prefix="bb/"
        )
        Image.objects.create(
            camera=self.camera,
            bucket="b",
            object_key="a/img1.jpg",
            datetime="2023-01-01T10:00:00Z",
        )
        Image.objects.create(
            camera=self.camera,
            bucket="b",
            object_key="a/img2.jpg",
            datetime="2023-06-15T12:00:00Z",
        )
        Image.objects.create(
            camera=self.other,
            bucket="b",
            object_key="bb/img3.jpg",
            datetime="2023-03-01T08:00:00Z",
        )

    def _url(self, **params):
        from urllib.parse import urlencode

        base = reverse("api-images")
        return f"{base}?{urlencode(params)}" if params else base

    def test_filter_by_camera_slug(self):
        response = self.client.get(self._url(camera="cam-a"))
        self.assertEqual(response.status_code, 200)
        keys = [r["filename"] for r in response.data["results"]]
        self.assertIn("img1.jpg", keys)
        self.assertIn("img2.jpg", keys)
        self.assertNotIn("img3.jpg", keys)

    def test_filter_by_date_after(self):
        response = self.client.get(self._url(camera="cam-a", date_after="2023-06-01"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["filename"], "img2.jpg")

    def test_filter_by_date_before(self):
        response = self.client.get(self._url(camera="cam-a", date_before="2023-03-01"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["filename"], "img1.jpg")

    def test_ordered_by_datetime(self):
        response = self.client.get(self._url(camera="cam-a"))
        datetimes = [r["datetime"] for r in response.data["results"]]
        self.assertEqual(datetimes, sorted(datetimes))

    def test_response_fields(self):
        response = self.client.get(self._url(camera="cam-a"))
        item = response.data["results"][0]
        self.assertEqual(
            set(item.keys()),
            {"id", "datetime", "filename", "width_px", "height_px", "rotation"},
        )


class PreviewServeTests(TestCase):
    """serve_image_preview and serve_image_thumbnail use pre-computed keys when available."""

    def setUp(self):
        self.factory = RequestFactory()
        self.camera = Camera.objects.create(
            camera_name="Test Cam", slug="test-cam", s3_bucket="bkt", s3_prefix="cam/"
        )
        self.image_bytes = self._jpeg_bytes((200, 150))
        self.image = Image.objects.create(
            camera=self.camera,
            bucket="bkt",
            object_key="cam/img.jpg",
            filename="img.jpg",
        )

    def _jpeg_bytes(self, size: tuple[int, int]) -> bytes:
        buf = BytesIO()
        PILImage.new("RGB", size, color=(100, 150, 200)).save(buf, format="JPEG")
        return buf.getvalue()

    def _patch_s3(self, return_bytes):
        return (
            patch("image_index.views.build_s3_client", return_value=Mock()),
            patch(
                "image_index.views.get_object_bytes",
                return_value=(return_bytes, "image/jpeg"),
            ),
        )

    def test_preview_uses_precomputed_key(self):
        pre = self._jpeg_bytes((640, 480))
        self.image.preview_object_key = "previews/cam/img.jpg"
        self.image.save()

        build_patch, get_patch = self._patch_s3(pre)
        with build_patch, get_patch as mock_get:
            response = serve_image_preview(self.factory.get("/"), self.image.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, pre)
        mock_get.assert_called_once_with(ANY, "bkt", "previews/cam/img.jpg")

    def test_preview_falls_back_to_original_when_no_precomputed_key(self):
        build_patch, get_patch = self._patch_s3(self.image_bytes)
        with build_patch, get_patch as mock_get:
            response = serve_image_preview(self.factory.get("/"), self.image.pk)

        self.assertEqual(response.status_code, 200)
        mock_get.assert_called_once_with(ANY, "bkt", "cam/img.jpg")

    def test_thumbnail_uses_precomputed_key(self):
        thumb = self._jpeg_bytes((160, 120))
        self.image.thumbnail_object_key = "thumbnails/cam/img.jpg"
        self.image.save()

        build_patch, get_patch = self._patch_s3(thumb)
        with build_patch, get_patch as mock_get:
            response = serve_image_thumbnail(self.factory.get("/"), self.image.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, thumb)
        mock_get.assert_called_once_with(ANY, "bkt", "thumbnails/cam/img.jpg")


@override_settings(
    S3_ENDPOINT_URL="http://fake-s3", S3_ACCESS_KEY="key", S3_SECRET_KEY="secret"
)
class GeneratePreviewsCommandTests(TestCase):
    def setUp(self):
        self.camera = Camera.objects.create(
            camera_name="Cam X", slug="cam-x", s3_bucket="bkt", s3_prefix="x/"
        )
        self.img1 = Image.objects.create(
            camera=self.camera,
            bucket="bkt",
            object_key="x/a.jpg",
            filename="a.jpg",
            datetime="2024-06-01T08:00:00Z",
        )
        self.img2 = Image.objects.create(
            camera=self.camera,
            bucket="bkt",
            object_key="x/b.jpg",
            filename="b.jpg",
            datetime="2024-06-02T08:00:00Z",
            preview_object_key="previews/x/b.jpg",
            thumbnail_object_key="thumbnails/x/b.jpg",
        )

    def _jpeg_bytes(self) -> bytes:
        buf = BytesIO()
        PILImage.new("RGB", (800, 600)).save(buf, format="JPEG")
        return buf.getvalue()

    def _s3_patches(self):
        return (
            patch(
                "image_index.management.commands.generate_previews.build_s3_client",
                return_value=Mock(),
            ),
            patch(
                "image_index.management.commands.generate_previews.get_object_bytes",
                return_value=(self._jpeg_bytes(), "image/jpeg"),
            ),
            patch("image_index.management.commands.generate_previews.put_object_bytes"),
        )

    def test_only_processes_images_without_preview_key(self):
        build_p, get_p, put_p = self._s3_patches()
        with build_p, get_p, put_p:
            call_command("generate_previews", camera_id=self.camera.pk)

        self.img1.refresh_from_db()
        self.img2.refresh_from_db()
        self.assertEqual(self.img1.preview_object_key, "previews/x/a.jpg")
        self.assertEqual(self.img1.thumbnail_object_key, "thumbnails/x/a.jpg")
        # img2 already had keys — should be unchanged
        self.assertEqual(self.img2.preview_object_key, "previews/x/b.jpg")

    def test_force_regenerates_existing_keys(self):
        build_p, get_p, put_p = self._s3_patches()
        with build_p, get_p, put_p as mock_put:
            call_command("generate_previews", camera_id=self.camera.pk, force=True)

        # put_object_bytes called twice per image (preview + thumbnail), 2 images → 4 calls
        self.assertEqual(mock_put.call_count, 4)

    def test_dry_run_does_not_update_db(self):
        build_p, get_p, put_p = self._s3_patches()
        with build_p, get_p, put_p:
            call_command("generate_previews", camera_id=self.camera.pk, dry_run=True)

        self.img1.refresh_from_db()
        self.assertIsNone(self.img1.preview_object_key)


class PresignedUrlViewTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.camera = Camera.objects.create(
            camera_name="Signed Cam",
            slug="signed-cam",
            s3_bucket="bkt",
            s3_prefix="sc/",
        )
        self.image = Image.objects.create(
            camera=self.camera,
            bucket="bkt",
            object_key="sc/img.jpg",
            filename="img.jpg",
        )

    def _patch_s3(self, signed_url="https://s3.example.com/signed"):
        return (
            patch("image_index.views.build_s3_client", return_value=Mock()),
            patch(
                "image_index.views.generate_presigned_url",
                return_value=signed_url,
            ),
        )

    def test_preview_url_returns_presigned_url_when_key_set(self):
        self.image.preview_object_key = "previews/sc/img.jpg"
        self.image.save()

        build_p, sign_p = self._patch_s3()
        with build_p, sign_p as mock_sign:
            response = self.client.get(f"/cams/images/{self.image.pk}/preview-url/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["url"], "https://s3.example.com/signed")
        self.assertEqual(data["expires_in"], 3600)
        mock_sign.assert_called_once_with(ANY, "bkt", "previews/sc/img.jpg")

    def test_preview_url_returns_proxy_fallback_when_no_key(self):
        build_p, sign_p = self._patch_s3()
        with build_p, sign_p as mock_sign:
            response = self.client.get(f"/cams/images/{self.image.pk}/preview-url/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn(f"/cams/images/{self.image.pk}/preview/", data["url"])
        mock_sign.assert_not_called()

    def test_thumbnail_url_returns_presigned_url_when_key_set(self):
        self.image.thumbnail_object_key = "thumbnails/sc/img.jpg"
        self.image.save()

        build_p, sign_p = self._patch_s3()
        with build_p, sign_p as mock_sign:
            response = self.client.get(f"/cams/images/{self.image.pk}/thumb-url/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["url"], "https://s3.example.com/signed")
        mock_sign.assert_called_once_with(ANY, "bkt", "thumbnails/sc/img.jpg")

    def test_thumbnail_url_returns_proxy_fallback_when_no_key(self):
        build_p, sign_p = self._patch_s3()
        with build_p, sign_p as mock_sign:
            response = self.client.get(f"/cams/images/{self.image.pk}/thumb-url/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn(f"/cams/images/{self.image.pk}/thumb/", data["url"])
        mock_sign.assert_not_called()


class IndexS3ImagesCommandTests(TestCase):
    CMD = "index_s3_images"
    BUCKET = "bkt"
    PREFIX = "cam1/"

    def setUp(self):
        self.camera = Camera.objects.create(
            camera_name="Test Cam",
            slug="test-cam",
            s3_bucket=self.BUCKET,
            s3_prefix=self.PREFIX,
        )

    def _jpeg_bytes(self) -> bytes:
        buf = BytesIO()
        PILImage.new("RGB", (100, 75)).save(buf, format="JPEG")
        return buf.getvalue()

    def _s3_obj(self, key: str, etag: str, last_modified) -> dict:
        return {
            "Key": key,
            "ETag": f'"{etag}"',
            "Size": 500,
            "LastModified": last_modified,
        }

    def _mock_s3(self, pages: list[dict]):
        mock_s3 = Mock()
        mock_paginator = Mock()
        mock_paginator.paginate.return_value = pages
        mock_s3.get_paginator.return_value = mock_paginator
        return mock_s3

    def _patches(self, mock_s3):
        return (
            patch(
                "image_index.management.commands.index_s3_images.build_s3_client",
                return_value=mock_s3,
            ),
            patch(
                "image_index.management.commands.index_s3_images.get_object_bytes",
                return_value=(self._jpeg_bytes(), "image/jpeg"),
            ),
        )

    def test_new_image_is_indexed(self):
        t = tz_now() - timedelta(hours=1)
        mock_s3 = self._mock_s3(
            [{"Contents": [self._s3_obj("cam1/img.jpg", "abc", t)]}]
        )
        build_p, get_p = self._patches(mock_s3)
        with build_p, get_p:
            call_command(self.CMD, camera_id=self.camera.pk)
        self.assertEqual(Image.objects.filter(camera=self.camera).count(), 1)

    def test_unchanged_etag_is_skipped(self):
        t = tz_now() - timedelta(hours=1)
        Image.objects.create(
            camera=self.camera,
            bucket=self.BUCKET,
            object_key="cam1/img.jpg",
            filename="img.jpg",
            s3_etag="abc",
            s3_last_modified=t,
        )
        mock_s3 = self._mock_s3(
            [{"Contents": [self._s3_obj("cam1/img.jpg", "abc", t)]}]
        )
        build_p, get_p = self._patches(mock_s3)
        with build_p, get_p as mock_get:
            call_command(self.CMD, camera_id=self.camera.pk)
        mock_get.assert_not_called()

    def test_incremental_no_watermark_does_full_scan(self):
        """With no images in DB, --incremental still indexes everything."""
        t = tz_now() - timedelta(days=30)
        mock_s3 = self._mock_s3(
            [{"Contents": [self._s3_obj("cam1/img.jpg", "abc", t)]}]
        )
        build_p, get_p = self._patches(mock_s3)
        with build_p, get_p:
            call_command(self.CMD, camera_id=self.camera.pk, incremental=True)
        self.assertEqual(Image.objects.filter(camera=self.camera).count(), 1)

    def test_incremental_skips_objects_before_buffer(self):
        """Objects older than watermark - buffer_days are skipped without an ETag check."""
        watermark = tz_now() - timedelta(days=10)
        Image.objects.create(
            camera=self.camera,
            bucket=self.BUCKET,
            object_key="cam1/watermark.jpg",
            filename="watermark.jpg",
            s3_etag="wm",
            s3_last_modified=watermark,
        )
        # 30 days ago: well before buffer_start (watermark - 7 days = 17 days ago)
        ancient = tz_now() - timedelta(days=30)
        mock_s3 = self._mock_s3(
            [{"Contents": [self._s3_obj("cam1/ancient.jpg", "old", ancient)]}]
        )
        build_p, get_p = self._patches(mock_s3)
        with build_p, get_p as mock_get:
            call_command(self.CMD, camera_id=self.camera.pk, incremental=True)
        # ancient object was skipped: not fetched, not added to DB
        mock_get.assert_not_called()
        self.assertFalse(Image.objects.filter(object_key="cam1/ancient.jpg").exists())

    def test_incremental_indexes_new_objects(self):
        """Objects newer than the watermark are fetched and indexed."""
        watermark = tz_now() - timedelta(days=2)
        Image.objects.create(
            camera=self.camera,
            bucket=self.BUCKET,
            object_key="cam1/old.jpg",
            filename="old.jpg",
            s3_etag="wm",
            s3_last_modified=watermark,
        )
        fresh = tz_now() - timedelta(hours=1)
        mock_s3 = self._mock_s3(
            [{"Contents": [self._s3_obj("cam1/new.jpg", "fresh", fresh)]}]
        )
        build_p, get_p = self._patches(mock_s3)
        with build_p, get_p:
            call_command(self.CMD, camera_id=self.camera.pk, incremental=True)
        self.assertTrue(Image.objects.filter(object_key="cam1/new.jpg").exists())

    def test_incremental_etag_match_within_buffer_is_skipped(self):
        """An object within the buffer window but with matching ETag is not re-fetched."""
        watermark = tz_now() - timedelta(days=1)
        within_buffer = tz_now() - timedelta(days=5)  # inside 7-day buffer
        # Watermark image (establishes max s3_last_modified)
        Image.objects.create(
            camera=self.camera,
            bucket=self.BUCKET,
            object_key="cam1/newest.jpg",
            filename="newest.jpg",
            s3_etag="wm",
            s3_last_modified=watermark,
        )
        # Image inside buffer with matching ETag
        Image.objects.create(
            camera=self.camera,
            bucket=self.BUCKET,
            object_key="cam1/buffer.jpg",
            filename="buffer.jpg",
            s3_etag="same",
            s3_last_modified=within_buffer,
        )
        mock_s3 = self._mock_s3(
            [
                {
                    "Contents": [
                        self._s3_obj("cam1/buffer.jpg", "same", within_buffer),
                    ]
                }
            ]
        )
        build_p, get_p = self._patches(mock_s3)
        with build_p, get_p as mock_get:
            call_command(self.CMD, camera_id=self.camera.pk, incremental=True)
        mock_get.assert_not_called()

    def test_key_filter_regex_excludes_non_matching_objects(self):
        """s3_key_filter_regex restricts indexing to keys matching the pattern."""
        self.camera.s3_key_filter_regex = r"/canon/rgb/"
        self.camera.save()
        t = tz_now() - timedelta(hours=1)
        mock_s3 = self._mock_s3(
            [
                {
                    "Contents": [
                        self._s3_obj("cam1/canon/rgb/img1.jpg", "abc", t),
                        self._s3_obj("cam1/pano/rgb/img2.jpg", "def", t),
                    ]
                }
            ]
        )
        build_p, get_p = self._patches(mock_s3)
        with build_p, get_p:
            call_command(self.CMD, camera_id=self.camera.pk)
        self.assertTrue(
            Image.objects.filter(object_key="cam1/canon/rgb/img1.jpg").exists()
        )
        self.assertFalse(
            Image.objects.filter(object_key="cam1/pano/rgb/img2.jpg").exists()
        )

    def test_inactive_camera_skipped_without_explicit_camera_id(self):
        """Cron-style runs (no --camera-id) only index active cameras."""
        self.camera.is_active = False
        self.camera.save()
        mock_s3 = self._mock_s3([{"Contents": []}])
        build_p, get_p = self._patches(mock_s3)
        with build_p, get_p, self.assertRaises(CommandError):
            call_command(self.CMD)
        mock_s3.get_paginator.assert_not_called()

    def test_inactive_camera_still_indexed_with_explicit_camera_id(self):
        """--camera-id bypasses is_active, for manual/backfill runs."""
        self.camera.is_active = False
        self.camera.save()
        t = tz_now() - timedelta(hours=1)
        mock_s3 = self._mock_s3(
            [{"Contents": [self._s3_obj("cam1/img.jpg", "abc", t)]}]
        )
        build_p, get_p = self._patches(mock_s3)
        with build_p, get_p:
            call_command(self.CMD, camera_id=self.camera.pk)
        self.assertEqual(Image.objects.filter(camera=self.camera).count(), 1)


class ParseDatetimeFromFilenameTests(TestCase):
    def test_primary_pattern(self):
        dt = parse_datetime_from_filename("prefix_20260901_070000_anything.jpg")
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour), (2026, 9, 1, 7))

    def test_alt_pattern_underscore_separated_date(self):
        dt = parse_datetime_from_filename("cam01_canon_rgb_2026_09_01_070000.jpg")
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour), (2026, 9, 1, 7))

    def test_no_match_returns_none(self):
        self.assertIsNone(parse_datetime_from_filename("not_a_timestamped_file.jpg"))
