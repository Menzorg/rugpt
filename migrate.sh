#!/bin/bash
# RuGPT Database Migration Script
# Runs SQL migrations against the rugpt database

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment variables
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Default values
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-rugpt}"
DB_USER="${DB_USER:-postgres}"

echo "==================================="
echo "RuGPT Database Migration"
echo "==================================="
echo "Host: $DB_HOST:$DB_PORT"
echo "Database: $DB_NAME"
echo "User: $DB_USER"
echo "==================================="

# Check if database exists, create if not
echo "Checking database..."
if ! psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
    echo "Creating database $DB_NAME..."
    psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -c "CREATE DATABASE $DB_NAME;"
    echo "Database created."
else
    echo "Database $DB_NAME exists."
fi

# Run migrations
echo ""
echo "Running migrations..."

for migration in src/engine/migrations/*.sql; do
    if [ -f "$migration" ]; then
        filename=$(basename "$migration")
        echo "  Running: $filename"
        psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -f "$migration" 2>&1 | grep -v "^NOTICE:" || true
        echo "  ✓ $filename completed"
    fi
done

echo ""
echo "==================================="
echo "Migrations complete!"
echo "==================================="
