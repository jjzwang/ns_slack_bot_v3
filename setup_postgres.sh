#!/usr/bin/env bash
# =============================================================================
# PostgreSQL Setup for NetSuite Gatekeeper (Self-Hosted)
# =============================================================================
# Run this once on your server to create the database and user.
#
# Usage:
#   chmod +x setup_postgres.sh
#   sudo -u postgres ./setup_postgres.sh
# =============================================================================

set -euo pipefail

DB_NAME="${GATEKEEPER_DB_NAME:-gatekeeper}"
DB_USER="${GATEKEEPER_DB_USER:-gatekeeper}"
DB_PASSWORD="${GATEKEEPER_DB_PASSWORD:-}"
DB_HOST="${PGHOST:-localhost}"
DB_PORT="${PGPORT:-5432}"

if [ -z "$DB_PASSWORD" ]; then
    DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
    echo "Generated random password for database user."
fi

echo "============================================="
echo "  NetSuite Gatekeeper — PostgreSQL Setup"
echo "============================================="
echo "  Database:  $DB_NAME"
echo "  User:      $DB_USER"
echo "  Host:      $DB_HOST"
echo "  Port:      $DB_PORT"
echo ""

echo "Creating database user '$DB_USER'..."
# Use psql --set variables + format('%I'/'%L') so PostgreSQL handles all quoting,
# preventing SQL injection from unusual usernames or passwords in env vars.
psql -h "$DB_HOST" -p "$DB_PORT" \
    --set=db_user="$DB_USER" \
    --set=db_pass="$DB_PASSWORD" \
    -v ON_ERROR_STOP=1 <<'EOSQL'
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = :'db_user') THEN
            EXECUTE format('CREATE ROLE %I WITH LOGIN PASSWORD %L', :'db_user', :'db_pass');
            RAISE NOTICE 'Created user: %', :'db_user';
        ELSE
            EXECUTE format('ALTER ROLE %I WITH PASSWORD %L', :'db_user', :'db_pass');
            RAISE NOTICE 'User % already exists — password updated.', :'db_user';
        END IF;
    END
    $$;
EOSQL

echo "Creating database '$DB_NAME'..."
psql -h "$DB_HOST" -p "$DB_PORT" \
    --set=db_name="$DB_NAME" \
    --set=db_user="$DB_USER" \
    -v ON_ERROR_STOP=1 <<'EOSQL'
    SELECT format('CREATE DATABASE %I OWNER %I', :'db_name', :'db_user')
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db_name')\gexec

    SELECT format('ALTER DATABASE %I OWNER TO %I', :'db_name', :'db_user')\gexec
EOSQL

echo "Granting permissions..."
psql -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" \
    --set=db_name="$DB_NAME" \
    --set=db_user="$DB_USER" \
    -v ON_ERROR_STOP=1 <<'EOSQL'
    SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', :'db_name', :'db_user')\gexec
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO :db_user;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO :db_user;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO :db_user;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO :db_user;
EOSQL

echo ""
echo "Verifying connection as '$DB_USER'..."
PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
    -c "SELECT 1 AS connected;" > /dev/null 2>&1

if [ $? -eq 0 ]; then
    echo "✅ Connection verified."
else
    echo "⚠️  Could not connect as $DB_USER. Check pg_hba.conf allows password auth."
    echo "    Add this to pg_hba.conf and restart PostgreSQL:"
    echo "    host    $DB_NAME    $DB_USER    127.0.0.1/32    md5"
fi

CONNECTION_STRING="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"

echo ""
echo "============================================="
echo "  Add this to your .env file:"
echo "============================================="
echo "  DATABASE_URL=$CONNECTION_STRING"
echo ""
echo "  Or use individual variables:"
echo "  PGHOST=$DB_HOST"
echo "  PGPORT=$DB_PORT"
echo "  PGDATABASE=$DB_NAME"
echo "  PGUSER=$DB_USER"
echo "  PGPASSWORD=$DB_PASSWORD"
echo "============================================="
echo "  The bot will create tables automatically"
echo "  on first startup (init_db)."
echo "============================================="
