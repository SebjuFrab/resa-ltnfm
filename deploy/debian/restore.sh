#!/bin/sh
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE=${ENV_FILE:-$PROJECT_DIR/.env.production}
COMPOSE_FILE=$PROJECT_DIR/compose.production.yaml
LOCK_FILE=${LOCK_FILE:-/run/lock/ltnm-reservations.lock}
EXPECTED_CONFIRMATION=resa-ltnfm.agrobio-bretagne.org

compose() {
    docker compose \
        --project-directory "$PROJECT_DIR" \
        --env-file "$ENV_FILE" \
        --file "$COMPOSE_FILE" \
        "$@"
}

if [ "$#" -ne 1 ]; then
    echo "Usage : CONFIRM_RESTORE=$EXPECTED_CONFIRMATION $0 /chemin/vers/ltnm-HORODATAGE" >&2
    exit 1
fi
if [ "${CONFIRM_RESTORE:-}" != "$EXPECTED_CONFIRMATION" ]; then
    echo "Restauration refusée : définissez CONFIRM_RESTORE=$EXPECTED_CONFIRMATION." >&2
    exit 1
fi

case "$1" in
    /*) ;;
    *)
        echo "Le chemin de sauvegarde doit être absolu." >&2
        exit 1
        ;;
esac
BACKUP_DIR=$(readlink -f "$1")
case "$BACKUP_DIR" in
    ""|/)
        echo "Chemin de sauvegarde invalide." >&2
        exit 1
        ;;
esac

for required_file in database.dump media.tar.gz SHA256SUMS; do
    if [ ! -f "$BACKUP_DIR/$required_file" ]; then
        echo "Fichier de sauvegarde absent : $BACKUP_DIR/$required_file" >&2
        exit 1
    fi
done
if [ ! -f "$ENV_FILE" ]; then
    echo "Fichier d'environnement absent : $ENV_FILE" >&2
    exit 1
fi
if find "$ENV_FILE" -prune -perm /077 -print | grep -q .; then
    echo "Le fichier $ENV_FILE doit être privé (chmod 600)." >&2
    exit 1
fi

command -v flock >/dev/null 2>&1 || {
    echo "La commande flock (paquet util-linux) est obligatoire." >&2
    exit 1
}
exec 9>"$LOCK_FILE"
flock -w 3600 9 || {
    echo "Impossible d'obtenir le verrou d'exploitation après une heure." >&2
    exit 1
}

(
    cd "$BACKUP_DIR"
    sha256sum --check SHA256SUMS
)
tar -tzf "$BACKUP_DIR/media.tar.gz" >/dev/null
if tar -tzf "$BACKUP_DIR/media.tar.gz" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    echo "L'archive média contient un chemin dangereux." >&2
    exit 1
fi

echo "La restauration va remplacer la base et les médias courants."
echo "Un instantané de sécurité doit avoir été créé et répliqué avant cette commande."

compose up --detach --wait --wait-timeout 120 db redis
compose stop --timeout 45 web
compose exec -T db sh /docker-entrypoint-initdb.d/10-init-app-db.sh

var_dir=$(readlink -f "$PROJECT_DIR/var")
media_dir=$(readlink -f "$PROJECT_DIR/var/media")
if [ "$media_dir" != "$var_dir/media" ]; then
    echo "Le chemin média résolu n'est pas le chemin attendu : $media_dir" >&2
    exit 1
fi
restore_stamp=$(date -u +%Y%m%dT%H%M%SZ)
previous_media="$var_dir/media.pre-restore-$restore_stamp"
if [ -e "$previous_media" ]; then
    echo "Le répertoire de secours existe déjà : $previous_media" >&2
    exit 1
fi

mv "$media_dir" "$previous_media"
mkdir -m 0755 "$media_dir"
tar -xzf "$BACKUP_DIR/media.tar.gz" --no-same-owner -C "$media_dir"
compose run --rm --no-deps --user 0 web \
    sh -c 'chown -R appuser:appuser /app/media && chmod 0755 /app/media'

compose exec -T db sh -c '
    set -eu
    dropdb --username "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB"
    createdb --username "$POSTGRES_USER" --owner "$POSTGRES_APP_USER" "$POSTGRES_DB"
'
compose exec -T db sh -c '
    PGPASSWORD="$POSTGRES_APP_PASSWORD" exec pg_restore \
        --host=127.0.0.1 \
        --username="$POSTGRES_APP_USER" \
        --dbname="$POSTGRES_DB" \
        --exit-on-error \
        --no-owner \
        --no-acl
' < "$BACKUP_DIR/database.dump"

compose run --rm web python manage.py migrate --noinput
compose exec -T redis redis-cli FLUSHDB >/dev/null
compose up --detach --remove-orphans --wait --wait-timeout 180
compose exec -T web python manage.py check --deploy

echo "Restauration terminée. Anciens médias conservés dans : $previous_media"
echo "Testez le site avant de supprimer manuellement ce répertoire de secours."
