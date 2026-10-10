#!/bin/sh
# Runs once on a fresh primary (docker-entrypoint-initdb.d). Idempotent, so it can also be
# run by hand against an existing volume:  docker compose exec postgres sh /docker-entrypoint-initdb.d/10-replication.sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'replicator') THEN
    CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'replicator';
  END IF;
END $$;
SQL
HBA="$PGDATA/pg_hba.conf"
grep -q "replication replicator" "$HBA" || echo "host replication replicator all scram-sha-256" >> "$HBA"
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "SELECT pg_reload_conf();" >/dev/null
