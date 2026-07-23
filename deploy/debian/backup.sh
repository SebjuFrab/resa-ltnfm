#!/bin/sh
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE=${ENV_FILE:-$PROJECT_DIR/.env.production}
COMPOSE_FILE=$PROJECT_DIR/compose.production.yaml
BACKUP_ROOT=${BACKUP_ROOT:-/var/backups/ltnm-reservations}
LOCK_FILE=${LOCK_FILE:-/run/lock/ltnm-reservations.lock}

compose() {
    docker compose \
        --project-directory "$PROJECT_DIR" \
        --env-file "$ENV_FILE" \
        --file "$COMPOSE_FILE" \
        "$@"
}

command -v flock >/dev/null 2>&1 || {
    echo "La commande flock (paquet util-linux) est obligatoire." >&2
    exit 1
}
exec 9>"$LOCK_FILE"
flock -w 3600 9 || {
    echo "Impossible d'obtenir le verrou d'exploitation après une heure." >&2
    exit 1
}

case "$BACKUP_ROOT" in
    /*) ;;
    *)
        echo "BACKUP_ROOT doit être un chemin absolu : $BACKUP_ROOT" >&2
        exit 1
        ;;
esac

mkdir -p "$BACKUP_ROOT"
BACKUP_ROOT=$(readlink -f "$BACKUP_ROOT")
case "$BACKUP_ROOT" in
    ""|/|/var|/var/backups)
        echo "Répertoire de sauvegarde trop large ou invalide : $BACKUP_ROOT" >&2
        exit 1
        ;;
esac

if [ ! -f "$ENV_FILE" ]; then
    echo "Fichier d'environnement absent : $ENV_FILE" >&2
    exit 1
fi
if find "$ENV_FILE" -prune -perm /077 -print | grep -q .; then
    echo "Le fichier $ENV_FILE doit être privé (chmod 600)." >&2
    exit 1
fi

chmod 0700 "$BACKUP_ROOT"
available_kb=$(df -Pk "$BACKUP_ROOT" | awk 'NR == 2 {print $4}')
database_bytes=$(compose exec -T db sh -c \
    'psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -tAc "SELECT pg_database_size(current_database())"' \
    | tr -d '[:space:]')
media_kb=$(du -sk "$PROJECT_DIR/var/media" | awk '{print $1}')
required_kb=$((database_bytes / 1024 + media_kb + 102400))
if [ "$available_kb" -lt "$required_kb" ]; then
    echo "Espace disque insuffisant pour créer l'instantané local." >&2
    exit 1
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
batch_name="ltnm-$stamp"
staging_dir="$BACKUP_ROOT/.$batch_name.part"
final_dir="$BACKUP_ROOT/$batch_name"

if [ -e "$staging_dir" ] || [ -e "$final_dir" ]; then
    echo "Une sauvegarde existe déjà pour l'horodatage $stamp." >&2
    exit 1
fi

mkdir -m 0700 "$staging_dir"
database_file="$staging_dir/database.dump"
media_file="$staging_dir/media.tar.gz"
checksum_file="$staging_dir/SHA256SUMS"

cleanup_partial() {
    rm -f "$database_file" "$media_file" "$checksum_file"
    rmdir "$staging_dir" 2>/dev/null || true
}
trap cleanup_partial EXIT
trap 'exit 1' HUP INT TERM

compose exec -T db sh -c \
    'exec pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --format=custom --no-owner --no-acl' \
    > "$database_file"
compose exec -T db pg_restore --list < "$database_file" >/dev/null

tar -czf "$media_file" -C "$PROJECT_DIR/var/media" .

(
    cd "$staging_dir"
    sha256sum database.dump media.tar.gz > SHA256SUMS
)

mv "$staging_dir" "$final_dir"
trap - EXIT HUP INT TERM

retention_days=$(sed -n 's/^BACKUP_RETENTION_DAYS=//p' "$ENV_FILE" | tail -n 1)
retention_days=${retention_days:-14}
case "$retention_days" in
    *[!0-9]*|""|0)
        echo "BACKUP_RETENTION_DAYS doit être un entier positif." >&2
        exit 1
        ;;
esac

find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'ltnm-*' \
    -mtime "+$retention_days" -print | while IFS= read -r expired_dir; do
        rm -f \
            "$expired_dir/database.dump" \
            "$expired_dir/media.tar.gz" \
            "$expired_dir/SHA256SUMS"
        rmdir "$expired_dir" 2>/dev/null || \
            echo "Ancienne sauvegarde non vide conservée : $expired_dir" >&2
    done

echo "Instantané local créé : $final_dir"
echo "Répliquez ce répertoire vers un stockage hors hôte chiffré et supervisé."
