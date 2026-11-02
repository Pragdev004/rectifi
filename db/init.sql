-- Rectifi database bootstrap. Safe to run again and again.
--
-- Docker runs this for you the first time the database volume is created.
-- Native Postgres: run it once as the postgres superuser, e.g.
--     psql -U postgres -f db/init.sql
--
-- It makes one login (rectifi) and three databases. The login is a PLAIN role on purpose: no superuser, no BYPASSRLS,
-- because Postgres skips row level security (the privacy check between people) for both. Run this as the postgres
-- superuser; keep that account for set-up and backups only, and let the app log in as rectifi.
--     rectifi        your real data
--     rectifi_test   wiped by the test suite on every run, never put real data here
--     rectifi_demo   fake data for trying the app (python scripts/dev.py demo)
-- The password below is for a database that only listens on your own laptop. Change it if you share the machine.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rectifi') THEN
        CREATE ROLE rectifi LOGIN PASSWORD 'rectifi_dev' CREATEDB NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
    ELSE
        ALTER ROLE rectifi CREATEDB NOSUPERUSER NOBYPASSRLS NOCREATEROLE;       -- an older login may have been a superuser
    END IF;
END
$$;

-- Stop here, loudly, if the login is still not plain (for example the role was created by a Docker volume that made it a
-- superuser: recreate that volume with the current docker-compose.yml).
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'rectifi' AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'The rectifi login is a superuser or can bypass row level security, so that security would be skipped. Use a plain role.';
    END IF;
END
$$;

SELECT 'CREATE DATABASE rectifi OWNER rectifi'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'rectifi') \gexec

SELECT 'CREATE DATABASE rectifi_test OWNER rectifi'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'rectifi_test') \gexec

SELECT 'CREATE DATABASE rectifi_demo OWNER rectifi'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'rectifi_demo') \gexec
