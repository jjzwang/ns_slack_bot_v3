#!/usr/bin/env bash
# =============================================================================
# PostgreSQL Setup for NetSuite Gatekeeper (Self-Hosted)
# =============================================================================
# Run this once on your server to create the database and user.
#
# Prerequisites:
#   - PostgreSQL 14+ installed and running
#   - You have access to the 'postgres' superuser (or equivalent)
#
# Usage:
#   chmod +x setup_postgres.sh
#   sudo -u postgres ./setup_postgres.sh
#
# Or if you have a password-based superuser:
#   PGPASSWORD=your_superuser_pw ./setup_postgres.sh
# =============================================================================

set -euo pipefail

# ─── Configuration (edit these if needed) ────────────────────────────────────
DB_NAME="${GATEKEEPER_DB_NAME:-gatekeeper}"
DB_USER="${GATEKEEPER_DB_USER:-gatekeeper}"
DB_PASSWORD="${GATEKEEPER_DB_PASSWORD:-}"
DB_HOST="${PGHOST:-localhost}"
DB_PORT="${PGPORT:-5432}"

# Generate a random password if none provided
if [ -z "$DB_PASSWORD" ]; then
    DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
    echo "Generated random password for database user."
fi

echo "============================================="
echo "  NetSuite Gatekeeper — PostgreSQL Setup"
echo "============================================="
echo ""
echo "  Database:  $DB_NAME"
echo "  User:      $DB_USER"
echo "  Host:      $DB_HOST"
echo "  Port:      $DB_PORT"
echo ""

# ─── Create user and database ────────────────────────────────────────────────

echo "Creating database user '$DB_USER'..."
psql -h "$DB_HOST" -p "$DB_PORT" -v ON_ERROR_STOP=1 <<-EOSQL
    -- Create user if not exists
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '$DB_USER') THEN
            CREATE ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASSWORD';
            RAISE NOTICE 'Created user: $DB_USER';
        ELSE
            ALTER ROLE $DB_USER WITH PASSWORD '$DB_PASSWORD';
            RAISE NOTICE 'User $DB_USER already exists — password updated.';
        END IF;
    END
    \$\$;
EOSQL

echo "Creating database '$DB_NAME'..."
psql -h "$DB_HOST" -p "$DB_PORT" -v ON_ERROR_STOP=1 <<-EOSQL
    -- Create database if not exists
    SELECT 'CREATE DATABASE $DB_NAME OWNER $DB_USER'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$DB_NAME')\gexec

    -- Ensure ownership
    ALTER DATABASE $DB_NAME OWNER TO $DB_USER;
EOSQL

echo "Granting permissions..."
psql -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -v ON_ERROR_STOP=1 <<-EOSQL
    GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $DB_USER;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $DB_USER;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $DB_USER;
EOSQL

# ─── Verify connection ──────────────────────────────────────────────────────

echo ""
echo "Verifying connection as '$DB_USER'..."
PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1 AS connected;" > /dev/null 2>&1

if [ $? -eq 0 ]; then
    echo "✅ Connection verified."
else
    echo "⚠️  Could not connect as $DB_USER. Check pg_hba.conf allows password auth for local connections."
    echo "    You may need to add this line to pg_hba.conf and restart PostgreSQL:"
    echo "    host    $DB_NAME    $DB_USER    127.0.0.1/32    md5"
fi

# ─── Print .env configuration ───────────────────────────────────────────────

CONNECTION_STRING="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"

echo ""
echo "============================================="
echo "  Add this to your .env file:"
echo "============================================="
echo ""
echo "  DATABASE_URL=$CONNECTION_STRING"
echo ""
echo "  Or use individual variables:"
echo ""
echo "  PGHOST=$DB_HOST"
echo "  PGPORT=$DB_PORT"
echo "  PGDATABASE=$DB_NAME"
echo "  PGUSER=$DB_USER"
echo "  PGPASSWORD=$DB_PASSWORD"
echo ""
echo "============================================="
echo "  The bot will create tables automatically"
echo "  on first startup (init_db)."
echo "============================================="
