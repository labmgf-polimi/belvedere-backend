#!/usr/bin/env bash
# Local PostGIS (compose.dev.yml) for tests and a working copy of the live DB.
#
# Usage: scripts/localdb.sh <command> [args]
#   test [ARGS]          run the test suite on the local database
#   run [ARGS]           runserver on the local copy
#   manage ARGS          any manage.py command on the local copy (e.g. dbshell)
#   pull-live            overwrite the LOCAL copy with the live data
#                        (live is only read, via pg_dump; dump kept in db-backups/)
#   stop                 stop the local database (its data is kept)
#
# The live server is never written to: every write goes into the container.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f compose.dev.yml)
BACKUP_DIR=db-backups
LOCAL_ENV=.env.local

die() { echo "error: $*" >&2; exit 1; }

start_local_db() { "${COMPOSE[@]}" up -d --wait; }

local_manage() {
    [[ -f $LOCAL_ENV ]] || cp .env.local.example "$LOCAL_ENV"
    start_local_db
    ENV_FILE=$LOCAL_ENV uv run python manage.py "$@"
}

dump_live() {
    [[ -f .env ]] || die ".env with the live DB_* credentials not found"
    # Live credentials from .env, parsed by decouple exactly as Django does.
    eval "$(uv run --quiet python - <<'PY'
import shlex
from decouple import Config, RepositoryEnv
c = Config(RepositoryEnv(".env"))
names = {"PGHOST": "DB_HOST", "PGPORT": "DB_PORT", "PGDATABASE": "DB_NAME",
         "PGUSER": "DB_USER", "PGPASSWORD": "DB_PASSWORD"}
for pg, key in names.items():
    print(f"export {pg}={shlex.quote(str(c(key)))}")
PY
)"
    mkdir -p "$BACKUP_DIR"
    DUMP="$BACKUP_DIR/belvedere_$(date +%Y%m%d_%H%M%S).dump"
    echo "Reading live DB $PGDATABASE@$PGHOST:$PGPORT (read-only) -> $DUMP"
    pg_dump --format=custom --no-owner --no-privileges --file="$DUMP"
}

load_into_local() {
    start_local_db
    echo "Replacing the LOCAL database (container on 127.0.0.1:5442) with $DUMP"
    "${COMPOSE[@]}" exec -T db psql -U belvedere -d postgres -q -v ON_ERROR_STOP=1 \
        -c "DROP DATABASE IF EXISTS belvedere WITH (FORCE)" \
        -c "CREATE DATABASE belvedere"
    "${COMPOSE[@]}" exec -T db pg_restore -U belvedere -d belvedere \
        --no-owner --no-privileges --exit-on-error <"$DUMP"
    # Bring the copy up to the repo's migrations.
    local_manage migrate
}

command=${1:-}
shift || true
case $command in
    test) local_manage test --noinput "$@" ;;
    run) local_manage runserver "$@" ;;
    manage) local_manage "$@" ;;
    pull-live) dump_live && load_into_local ;;
    stop) "${COMPOSE[@]}" stop ;;
    *) sed -n '4,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
