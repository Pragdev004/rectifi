#!/bin/sh
# Runs once, when the database volume is new (the postgres image runs everything in /docker-entrypoint-initdb.d).
# Makes `rectifi`, the app's login, a PLAIN role: no superuser, no BYPASSRLS, so row level security applies to it.
# POSTGRES_USER (postgres) stays the superuser for set-up and backups. The password comes from RECTIFI_DB_PASSWORD
# (.env.deploy); it is passed to psql as a variable, so it is never written to a file or a log.
# (The image may source this file instead of running it, so it uses no "set -e" and ends with the psql exit code.)

if [ -z "${RECTIFI_DB_PASSWORD:-}" ]; then
    echo "RECTIFI_DB_PASSWORD is empty: set it in .env.deploy" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 -v app_password="$RECTIFI_DB_PASSWORD" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
CREATE ROLE rectifi LOGIN PASSWORD :'app_password' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER DATABASE rectifi OWNER TO rectifi;
EOSQL
