#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE=${ENV_FILE:-$PROJECT_DIR/.env.production}
COMPOSE_FILE=$PROJECT_DIR/compose.production.yaml
LOCK_FILE=${LOCK_FILE:-/run/lock/ltnm-reservations.lock}

command -v flock >/dev/null 2>&1 || {
    echo "La commande flock (paquet util-linux) est obligatoire." >&2
    exit 1
}
exec 9>"$LOCK_FILE"
echo "Attente du verrou d'exploitation $LOCK_FILE..."
flock -w 3600 9 || {
    echo "Impossible d'obtenir le verrou après une heure." >&2
    exit 1
}

compose() {
    docker compose \
        --project-directory "$PROJECT_DIR" \
        --env-file "$ENV_FILE" \
        --file "$COMPOSE_FILE" \
        "$@"
}

if [ ! -f "$ENV_FILE" ]; then
    echo "Fichier d'environnement absent : $ENV_FILE" >&2
    echo "Copiez .env.production.example vers .env.production puis renseignez les secrets." >&2
    exit 1
fi

if find "$ENV_FILE" -prune -perm /077 -print | grep -q .; then
    echo "Le fichier $ENV_FILE doit être privé (chmod 600)." >&2
    exit 1
fi

if grep -Eq '(^|=)change-me($|@)|smtp\.example\.org' "$ENV_FILE"; then
    echo "Le fichier $ENV_FILE contient encore au moins une valeur d'exemple." >&2
    exit 1
fi

env_value() {
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

django_secret=$(env_value DJANGO_SECRET_KEY)
admin_password=$(env_value POSTGRES_ADMIN_PASSWORD)
app_password=$(env_value POSTGRES_APP_PASSWORD)
app_release=$(env_value APP_RELEASE)
if [ "$django_secret" = "$admin_password" ] || \
   [ "$django_secret" = "$app_password" ] || \
   [ "$admin_password" = "$app_password" ]; then
    echo "Les secrets Django, PostgreSQL admin et PostgreSQL applicatif doivent différer." >&2
    exit 1
fi
if ! printf '%s' "$app_release" | grep -Eq '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'; then
    echo "APP_RELEASE n'est pas un tag d'image Docker valide." >&2
    exit 1
fi

command -v docker >/dev/null 2>&1 || {
    echo "Docker n'est pas installé ou absent du PATH." >&2
    exit 1
}
docker compose version >/dev/null

cd "$PROJECT_DIR"
mkdir -p var/media

echo "1/7 - Validation de la configuration Compose"
compose config --quiet

echo "2/7 - Construction de l'image applicative"
compose build --pull web

echo "3/7 - Préparation du stockage des médias"
compose run --rm --no-deps --user 0 web \
    sh -c 'chown appuser:appuser /app/media && chmod 0755 /app/media'

echo "4/7 - Démarrage de PostgreSQL et Redis"
compose up --detach --wait --wait-timeout 120 db redis
if compose ps --status running --services | grep -qx web; then
    compose stop --timeout 45 web
fi
compose exec -T db sh /docker-entrypoint-initdb.d/10-init-app-db.sh

echo "5/7 - Contrôles et migrations de la release"
compose run --rm web python manage.py check --deploy
compose run --rm web python manage.py migrate --plan
compose run --rm web python manage.py migrate --noinput

echo "6/7 - Démarrage de l'application"
compose up --detach --remove-orphans --wait --wait-timeout 180

echo "7/7 - Contrôle final"
compose exec -T web python manage.py check --deploy
compose exec -T web python manage.py migrate --check
compose ps

app_port=$(sed -n 's/^APP_HTTP_PORT=//p' "$ENV_FILE" | tail -n 1)
app_port=${app_port:-18001}
echo "Déploiement prêt sur http://127.0.0.1:$app_port."
echo "Vérifiez ensuite https://resa-ltnfm.agrobio-bretagne.org/healthz/."
