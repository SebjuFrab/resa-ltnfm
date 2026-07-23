#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE=${ENV_FILE:-$PROJECT_DIR/.env.production}
LOCK_FILE=${LOCK_FILE:-/run/lock/ltnm-reservations.lock}

command -v flock >/dev/null 2>&1 || {
    echo "La commande flock (paquet util-linux) est obligatoire." >&2
    exit 1
}
exec 9>"$LOCK_FILE"
flock -w 3600 9 || {
    echo "Impossible d'obtenir le verrou d'exploitation après une heure." >&2
    exit 1
}

docker compose \
    --project-directory "$PROJECT_DIR" \
    --env-file "$ENV_FILE" \
    --file "$PROJECT_DIR/compose.production.yaml" \
    exec -T web python manage.py anonymize_old_registrations
