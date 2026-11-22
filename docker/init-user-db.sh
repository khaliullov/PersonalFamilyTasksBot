#!/bin/bash
set -e

# Creates additional databases if specified in POSTGRES_MULTIPLE_DATABASES (comma-separated)
# e.g. POSTGRES_MULTIPLE_DATABASES=family_tasks_bot,project_b_db,project_c_db
if [ -n "$POSTGRES_MULTIPLE_DATABASES" ]; then
    for db in $(echo "$POSTGRES_MULTIPLE_DATABASES" | tr ',' ' '); do
        echo "Checking/creating database: $db"
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
            SELECT 'CREATE DATABASE $db' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db')\gexec
EOSQL
    done
fi
