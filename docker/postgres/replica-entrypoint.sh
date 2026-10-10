#!/bin/sh
# Streaming read replica: clone the primary once with pg_basebackup, then run as a hot standby.
set -eu
PGDATA=/var/lib/postgresql/data
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -h postgres -U gateway -q; do echo "waiting for primary"; sleep 1; done
  rm -rf "${PGDATA:?}"/*
  # -R writes standby.signal + primary_conninfo; -X stream copies WAL during the backup.
  PGPASSWORD=replicator pg_basebackup -h postgres -U replicator -D "$PGDATA" -R -X stream
  chown -R postgres:postgres "$PGDATA"
  chmod 700 "$PGDATA"
fi
exec su-exec postgres postgres -c hot_standby=on
