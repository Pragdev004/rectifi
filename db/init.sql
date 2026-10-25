-- Rectifi database bootstrap. Safe to run again and again.
--
-- Docker runs this for you the first time the database volume is created.
-- Native Postgres: run it once as the postgres superuser, e.g.
--     psql -U postgres -f db/init.sql
--
-- It makes one login (rectifi) and three databases:
--     rectifi        your real data
--     rectifi_test   wiped by the test suite on every run, never put real data here
--     rectifi_demo   fake data for trying the app (python scripts/dev.py demo)
-- The password below is for a database that only listens on your own laptop. Change it if you share the machine.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rectifi') THEN
        CREATE ROLE rectifi LOGIN PASSWORD 'rectifi_dev' CREATEDB;
    ELSE
        ALTER ROLE rectifi CREATEDB;
    END IF;
END
$$;

SELECT 'CREATE DATABASE rectifi OWNER rectifi'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'rectifi') \gexec

SELECT 'CREATE DATABASE rectifi_test OWNER rectifi'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'rectifi_test') \gexec

SELECT 'CREATE DATABASE rectifi_demo OWNER rectifi'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'rectifi_demo') \gexec
