import os
import sys
from pathlib import Path

from decouple import Config, RepositoryEnv, config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# ENV_FILE replaces .env entirely (e.g. .env.local for the local database), so
# no production value can leak in. Process environment variables still win.
if os.environ.get("ENV_FILE"):
    config = Config(RepositoryEnv(os.environ["ENV_FILE"]))  # noqa: F811

SECRET_KEY = config("SECRET_KEY", default="dev-secret-key")
DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*", cast=str).split(",")
CSRF_TRUSTED_ORIGINS = config(
    "CSRF_TRUSTED_ORIGINS",
    default="http://localhost:8000,http://127.0.0.1:8000",
).split(",")

# Trust the X-Forwarded-Proto header set by the reverse proxy (Dokploy/Traefik/Nginx)
# so that Django generates https:// URLs in API responses when behind TLS termination.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

S3_ENDPOINT_URL = config("S3_ENDPOINT_URL", default="")
S3_ACCESS_KEY = config("S3_ACCESS_KEY", default="")
S3_SECRET_KEY = config("S3_SECRET_KEY", default="")
S3_READONLY_ACCESS_KEY = config("S3_READONLY_ACCESS_KEY", default="")
S3_READONLY_SECRET_KEY = config("S3_READONLY_SECRET_KEY", default="")
S3_BUCKET_NAME = config("S3_BUCKET_NAME", default="belvedere-images")
S3_PRODUCTS_BUCKET_NAME = config(
    "S3_PRODUCTS_BUCKET_NAME", default="belvedere-products"
)
S3_REGION_NAME = config("S3_REGION_NAME", default="nbg1")
# Measurement photos: private bucket, objects under this prefix. Local
# development sets a separate prefix (e.g. dev/measurement_photos/) so test
# uploads never mix with real photos.
S3_SURVEYS_BUCKET_NAME = config("S3_SURVEYS_BUCKET_NAME", default="belvedere-surveys")
S3_MEASUREMENT_PHOTOS_PREFIX = config(
    "S3_MEASUREMENT_PHOTOS_PREFIX", default="measurement_photos/"
)

# Email, used by the admin password reset. The console backend prints messages
# (reset link included) to the server log, so nothing is sent until SMTP is set.
EMAIL_BACKEND = config(
    "EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = config("EMAIL_HOST", default="localhost")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_USE_SSL = config("EMAIL_USE_SSL", default=False, cast=bool)
# A hung SMTP server must fail the request before gunicorn's 60 s timeout.
EMAIL_TIMEOUT = config("EMAIL_TIMEOUT", default=10, cast=int)
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="webmaster@localhost")
PASSWORD_RESET_TIMEOUT = config("PASSWORD_RESET_TIMEOUT", default=3600, cast=int)
# The only login page; also the "Log in" link shown after a password reset.
LOGIN_URL = "admin:login"

# Optional path to a PNG logo file composited onto generated previews (bottom-right corner).
PREVIEW_LOGO_PATH = config("PREVIEW_LOGO_PATH", default="")

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "django.contrib.postgres",
    "import_export",
    "corsheaders",
    "rest_framework",
    "surveys",
    "image_index",
    "georef",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

_cors_origins = config("CORS_ALLOWED_ORIGINS", default="")
if _cors_origins:
    CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins.split(",") if o.strip()]
else:
    CORS_ALLOW_ALL_ORIGINS = True

REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": "300/minute"},
}

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": config("DB_NAME", default="belvedere"),
        "USER": config("DB_USER", default="belvedere"),
        "PASSWORD": config("DB_PASSWORD", default="belvedere"),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
        "OPTIONS": {"options": "-c search_path=public"},
    }
}

# The test runner creates and drops a test database on whatever server DB_HOST
# names; never let that be the shared production server by accident.
if (
    len(sys.argv) > 1
    and sys.argv[1] == "test"
    and DATABASES["default"]["HOST"] not in ("localhost", "127.0.0.1", "::1")
    and not config("ALLOW_REMOTE_TEST_DB", default=False, cast=bool)
):
    sys.exit(
        f"Refusing to run tests against remote DB_HOST={DATABASES['default']['HOST']}. "
        "Use scripts/localdb.sh test, or set ALLOW_REMOTE_TEST_DB=True."
    )


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Measurement photos use their own S3 storage (surveys.models.photo_storage);
# "default" only exists so nothing else can fail on a missing storage.
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
