#!/bin/sh
set -eu

: "${POSTGRES_APP_USER:?POSTGRES_APP_USER is required}"
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"

if [ "$POSTGRES_APP_USER" = "$POSTGRES_USER" ]; then
    echo "POSTGRES_APP_USER must differ from POSTGRES_USER." >&2
    exit 1
fi
if [ "$POSTGRES_APP_PASSWORD" = "$POSTGRES_PASSWORD" ]; then
    echo "The application and administration passwords must differ." >&2
    exit 1
fi
if [ "${#POSTGRES_PASSWORD}" -lt 20 ]; then
    echo "POSTGRES_PASSWORD must contain at least 20 characters." >&2
    exit 1
fi
if [ "${#POSTGRES_APP_PASSWORD}" -lt 20 ]; then
    echo "POSTGRES_APP_PASSWORD must contain at least 20 characters." >&2
    exit 1
fi

psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 \
    --set=app_user="$POSTGRES_APP_USER" \
    --set=app_password="$POSTGRES_APP_PASSWORD" <<'EOSQL'
SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
    :'app_user',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
\gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
    :'app_user',
    :'app_password'
)
\gexec

SELECT format('ALTER DATABASE %I OWNER TO %I', current_database(), :'app_user')
\gexec

SELECT format('ALTER SCHEMA public OWNER TO %I', :'app_user')
\gexec
EOSQL
