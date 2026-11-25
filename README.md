# belvedere-backend

Django API backend for the Belvedere survey and image indexing system.

## Stack

- **Python** ≥ 3.12
- **Django** ≥ 5.0 + Django REST Framework
- **PostgreSQL** + PostGIS (via `psycopg` v3)
- **AWS S3** via `django-storages`
- **Gunicorn** (production server)
- **Docker**

## Requirements

- Python ≥ 3.12
- PostgreSQL with PostGIS extension enabled
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`

## Installation

```bash
git clone https://github.com/franioli/belvedere-backend.git
cd belvedere-backend

# Install dependencies
uv sync
# or: pip install -e .

# Copy and configure environment variables
cp .env.example .env
# Edit .env with your DB credentials, AWS keys, secret key, etc.

# Apply migrations
uv run python manage.py migrate

# Create superuser
uv runpython manage.py createsuperuser

# Run development server 
uv run python manage.py runserver
```

### Production

Collect static files for production:

```bash
uv run python manage.py collectstatic --noinput
```

Run Gunicorn for production:

```bash
uv run gunicorn config.wsgi:application --bind 0.0.0.0:8000
``` 
Change the bind address and port as needed (e.g., if 8000 is already in use). Optionally, add options for workers, logging, etc. (see .env.example for example Gunicorn configuration).


### Docker

```bash
docker build -t belvedere-backend .
docker run --env-file .env -p 8000:8000 belvedere-backend
```

## Environment Variables

| Variable | Description |
|---|---|
| `SECRET_KEY` | Django secret key |
| `DEBUG` | `True` / `False` |
| `ALLOWED_HOSTS` | Comma-separated hostnames |
| `DATABASE_URL` | PostgreSQL connection string |
| `AWS_ACCESS_KEY_ID` | AWS credentials |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials |
| `AWS_STORAGE_BUCKET_NAME` | S3 bucket name |
| `AWS_S3_REGION_NAME` | S3 region |

## Project Structure

```
belvedere-backend/
├── config/                  # Django project settings
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
├── surveys/                 # Core app: campaigns, points, measurements
│   ├── models.py
│   ├── admin.py
│   ├── views.py
│   ├── serializers.py
│   └── migrations/
├── api/                     # API routing and views
├── image_index/             # Image indexing app
├── staticfiles/             # Collected static files
├── manage.py
├── pyproject.toml
└── Dockerfile
```

## Apps

| App | Purpose |
|---|---|
| `surveys` | Campaigns, survey points, measurements, photos |
| `api` | REST API endpoints and routing |
| `image_index` | Image metadata and indexing |

## Admin

The Django admin panel is available at `/admin/`. It supports CSV import/export for measurements (via `django-import-export`) and image upload for measurement photos stored in S3.

## Local Development (tests and a copy of the live data)

`compose.dev.yml` runs a PostGIS pinned to the server's exact versions
(PostgreSQL 18.4, PostGIS 3.6.4, PROJ 9.6.0) on `127.0.0.1:5442`.
`scripts/localdb.sh` drives it, using `.env.local` (created from
`.env.local.example`) instead of `.env`, so nothing points at production.

```bash
scripts/localdb.sh test                   # full test suite on the local database
scripts/localdb.sh test surveys.tests.X   # a single test
scripts/localdb.sh pull-live              # overwrite the LOCAL copy with live data (live is only read)
scripts/localdb.sh run                    # runserver on the local copy
scripts/localdb.sh manage dbshell         # any manage.py command on the local copy
scripts/localdb.sh stop
```

Plain `manage.py test` refuses to run when `DB_HOST` is not local, so the test
runner never creates databases on the shared server (override with
`ALLOW_REMOTE_TEST_DB=True`). Tests build their own database from migrations
and do not need a restored copy.