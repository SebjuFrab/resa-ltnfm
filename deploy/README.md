# Déploiement Debian — `resa-ltnfm.agrobio-bretagne.org`

Cette procédure installe l'application sous `/opt/resa-ltnfm` avec un projet Docker
Compose dédié. Le port applicatif n'écoute que sur `127.0.0.1:18001` ; PostgreSQL et
Redis ne publient aucun port. Le reverse proxy déjà présent sur le serveur conserve
donc les ports `80/443` et peut continuer à héberger les autres outils.

## Architecture retenue

```text
Internet
   |
   v
Nginx hôte :80/:443 + certificat Let's Encrypt
   |
   +--> 127.0.0.1:18001 --> Gunicorn/Django
                              |
                              +--> réseau Docker interne --> PostgreSQL
                              +--> réseau Docker interne --> Redis

/opt/resa-ltnfm/var/media --> bind mount Django + lecture Nginx
/var/backups/ltnm-reservations --> sauvegardes locales à répliquer hors hôte
```

Le nom Compose `ltnm_resa`, les volumes, le port de boucle locale et les limites de
ressources évitent les collisions avec les autres applications du Debian.

## 1. Prérequis DNS et réseau

1. Faire pointer l'enregistrement `A` de `resa-ltnfm.agrobio-bretagne.org` vers
   l'adresse IPv4 publique du serveur.
2. Ne publier un enregistrement `AAAA` que si IPv6 arrive réellement jusqu'à Nginx
   sur les ports `80` et `443`.
3. Autoriser au pare-feu uniquement les ports nécessaires, habituellement `22`,
   `80` et `443`. Ne pas ouvrir `18001`, `5432` ou `6379`.
4. Vérifier que le port local choisi est libre :

```bash
sudo ss -ltnp | grep ':18001' || true
```

La publication Docker est explicitement liée à `127.0.0.1`; cela reste nécessaire
même avec un pare-feu, car les règles de publication Docker ont leur propre chemin
réseau.

## 2. Auditer l'existant, puis installer ce qui manque

Sur un serveur partagé, commencer par identifier les composants déjà exploités :

```bash
docker compose version || true
sudo systemctl status docker --no-pager || true
sudo ss -ltnp | grep -E ':(80|443)\b' || true
sudo systemctl status nginx caddy traefik --no-pager || true
```

Si Docker et son plugin Compose sont déjà installés, conserver cette installation.
Toute mise à niveau du démon Docker doit être planifiée avec l'administrateur, car
elle peut redémarrer les conteneurs des autres outils. Sur un Debian neuf uniquement,
les commandes suivantes installent Docker depuis son dépôt APT officiel :

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl util-linux
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo docker compose version
```

Références : [installation Docker Engine sur Debian](https://docs.docker.com/engine/install/debian/)
et [plugin Docker Compose](https://docs.docker.com/compose/install/linux/).

Si Nginx, Caddy ou Traefik est déjà géré par l'administrateur du serveur, ne pas
installer une deuxième terminaison TLS : conserver l'outil existant et simplement
router ce domaine vers `http://127.0.0.1:18001` avec les en-têtes décrits plus bas.
Dans le seul cas où aucun proxy n'existe encore et où Nginx est retenu :

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx
sudo systemctl enable --now nginx
```

## 3. Installer les fichiers de l'application

Placer une copie versionnée du dépôt dans `/opt/resa-ltnfm`. Le mécanisme de copie
dépend du dépôt de code utilisé ; le résultat attendu est :

```bash
sudo install -d -m 0755 /opt/resa-ltnfm
cd /opt/resa-ltnfm
sudo cp -n .env.production.example .env.production
sudo chmod 600 .env.production
sudo chmod 755 deploy/debian/*.sh deploy/postgres/init-app-db.sh
```

Générer trois secrets différents, puis modifier `.env.production` avec `sudoedit` :

```bash
openssl rand -hex 32   # DJANGO_SECRET_KEY
openssl rand -hex 32   # POSTGRES_ADMIN_PASSWORD
openssl rand -hex 32   # POSTGRES_APP_PASSWORD
sudoedit /opt/resa-ltnfm/.env.production
```

Remplacer chaque `change-me` et `smtp.example.org`. Donner aussi à `APP_RELEASE` un
tag unique à chaque version, par exemple une date suivie du numéro de release.
Renseigner le relais SMTP réel,
l'expéditeur autorisé et le contact de l'organisation. Pour le port `587`, garder
`EMAIL_USE_TLS=1` et `EMAIL_USE_SSL=0`; pour le port `465`, faire l'inverse. Un seul
des deux doit être actif. Si un secret SMTP contient `$` ou `#`, l'entourer de quotes
simples dans `.env.production` afin d'éviter son interpolation par Compose.

Le compte `POSTGRES_ADMIN_USER` initialise le cluster. Django utilise
`POSTGRES_APP_USER`, créé sans super-pouvoir, création de base/rôle, réplication ni
contournement RLS. L'image PostgreSQL initialise ce rôle sur un volume neuf, puis le
script de déploiement rejoue l'étape idempotente pour synchroniser son mot de passe.
Pour une base déjà existante, faire auditer les propriétaires avant de basculer :

```bash
sudo docker compose --env-file .env.production -f compose.production.yaml \
  exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles ORDER BY rolname;"'
```

La ligne attendue pour `ltnm_app` contient uniquement `false` pour les privilèges.
Ne jamais supprimer un ancien rôle ou réattribuer ses objets sans
sauvegarde et sans revue PostgreSQL.

Pour changer le mot de passe applicatif, modifier `POSTGRES_APP_PASSWORD`, puis
relancer `deploy.sh`. Pour le compte administrateur, changer d'abord le mot de passe
dans PostgreSQL avec une session `psql` interactive (`\password`), puis seulement
mettre à jour `POSTGRES_ADMIN_PASSWORD`. Les noms des deux rôles doivent rester
différents et les trois secrets générés doivent tous être distincts.

Les indicateurs HSTS `includeSubDomains` et `preload` ne concernent que le nom
`resa-ltnfm.agrobio-bretagne.org` et ses éventuels sous-domaines, pas les autres
outils placés sur des domaines frères. Ne les désactiver qu'après revue de la
politique HTTPS ; Django signalera alors deux avertissements avec `check --deploy`.

## 4. Premier déploiement

Les statiques sont générés dans l'image pendant le build et ne sont pas modifiables
par Gunicorn. Le script arrête l'ancien service web, vérifie le plan, applique les
migrations comme une étape de release unique, puis démarre la nouvelle image :

```bash
cd /opt/resa-ltnfm
sudo ./deploy/debian/deploy.sh
```

Les dépendances sont attendues via leurs healthchecks avant Django, conformément au
[fonctionnement de l'ordre de démarrage Compose](https://docs.docker.com/compose/how-tos/startup-order/).

Créer ensuite le premier compte administrateur :

```bash
sudo docker compose --env-file .env.production -f compose.production.yaml \
  exec web python manage.py createsuperuser
```

Ne pas exécuter `seed_demo` en production.

## 5. Reverse proxy et HTTPS

Si aucun proxy n'existait, la configuration fournie est une configuration HTTP
initiale compatible avec Certbot. Les gardes évitent d'écraser une configuration
déjà modifiée par Certbot :

```bash
if [ ! -e /etc/nginx/sites-available/resa-ltnfm.agrobio-bretagne.org.conf ]; then
  sudo install -m 0644 deploy/nginx/resa-ltnfm.agrobio-bretagne.org.conf \
    /etc/nginx/sites-available/resa-ltnfm.agrobio-bretagne.org.conf
fi
if [ ! -e /etc/nginx/sites-enabled/resa-ltnfm.agrobio-bretagne.org.conf ]; then
  sudo ln -s /etc/nginx/sites-available/resa-ltnfm.agrobio-bretagne.org.conf \
    /etc/nginx/sites-enabled/resa-ltnfm.agrobio-bretagne.org.conf
fi
sudo nginx -t
sudo systemctl reload nginx

sudo certbot --nginx --redirect -d resa-ltnfm.agrobio-bretagne.org
sudo certbot renew --dry-run
```

Si le serveur possède déjà son propre proxy, reproduire impérativement ces règles :

- backend `http://127.0.0.1:18001` uniquement ;
- remplacer `Host` et `X-Forwarded-Host` par le domaine reçu ;
- remplacer `X-Forwarded-Proto` par le schéma traité au proxy ;
- remplacer `X-Forwarded-For` par l'adresse cliente observée au proxy, sans concaténer
  un en-tête fourni par le client ;
- servir `/opt/resa-ltnfm/var/media/` sous `/media/`, sans index de répertoire ;
- bloquer l'accès public à `/readyz/`, sauf depuis une IP de supervision explicitement
  autorisée ;
- ne pas journaliser la query string des écrans staff.

Ces contraintes sont importantes car Django fait confiance à ce proxy pour HTTPS et
pour la limitation de débit. Le backend ne doit jamais être joignable directement.

## 6. Vérifications après mise en ligne

```bash
curl --fail --silent --show-error \
  https://resa-ltnfm.agrobio-bretagne.org/healthz/
curl --fail --silent --show-error \
  -H 'Host: resa-ltnfm.agrobio-bretagne.org' \
  -H 'X-Forwarded-Proto: https' \
  http://127.0.0.1:18001/readyz/
curl -I https://resa-ltnfm.agrobio-bretagne.org/static/css/app.css

sudo docker compose --env-file .env.production -f compose.production.yaml \
  config --quiet
sudo docker compose --env-file .env.production -f compose.production.yaml ps
sudo docker compose --env-file .env.production -f compose.production.yaml \
  logs --tail=100 web
```

Vérifier aussi manuellement : connexion `/admin/login/`, page `/operations/`, upload
et affichage d'une image d'animation, création d'un groupe de test puis envoi SMTP.
Contrôler les en-têtes `Strict-Transport-Security`, `X-Content-Type-Options`,
`Referrer-Policy` et `X-Frame-Options` sur une réponse Django.

`/healthz/` confirme publiquement que le processus HTTP répond. `/readyz/` vérifie en
plus un `SELECT 1` PostgreSQL et un aller-retour Redis, sans exposer le détail d'une
panne ; Nginx le réserve donc au serveur. Ajouter un contrôle externe HTTPS avec
alerte sur `/healthz/`, ainsi qu'un contrôle interne de `/readyz/`, de l'expiration du
certificat et de l'espace disque. Une IP de supervision externe peut être ajoutée aux
directives `allow` de la location Nginx.

Si `APP_HTTP_PORT` change pour éviter une collision locale, modifier simultanément le
`proxy_pass` Nginx. Le domaine, le port et le chemin média fournis forment une
configuration cohérente et ne doivent pas être modifiés séparément.

## 7. Démarrage automatique et maintenance RGPD

Après un premier déploiement réussi, installer les unités fournies :

```bash
sudo install -m 0644 deploy/systemd/ltnm-*.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/ltnm-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/ltnm-*.service \
  /etc/systemd/system/ltnm-*.timer
sudo systemctl enable --now ltnm-reservations.service
sudo systemctl enable --now ltnm-backup.timer ltnm-maintenance.timer
sudo systemctl list-timers 'ltnm-*'
```

Les scripts utilisent tous le verrou `/run/lock/ltnm-reservations.lock`; une
sauvegarde, une restauration, une migration et l'anonymisation ne peuvent donc pas se
chevaucher. Le timer de maintenance exécute chaque jour
`anonymize_old_registrations` avec la
durée `DATA_RETENTION_DAYS`. Contrôler régulièrement :

```bash
sudo systemctl status ltnm-maintenance.service
sudo journalctl -u ltnm-maintenance.service --since yesterday
sudo systemctl status ltnm-backup.service
sudo systemctl --failed
```

Relier les échecs de ces unités au système d'alerte déjà utilisé sur le serveur ; un
timer systemd en erreur sans notification n'est pas une supervision suffisante.

## 8. Sauvegardes et restauration

Le timer crée chaque jour un répertoire atomique
`/var/backups/ltnm-reservations/ltnm-HORODATAGE/` contenant :

- un `pg_dump` PostgreSQL au format custom ;
- une archive des médias ;
- un manifeste SHA-256.

Ces instantanés locaux expirent après `BACKUP_RETENTION_DAYS`. Ils ne constituent pas
à eux seuls une sauvegarde : les répliquer avec
l'outil de sauvegarde déjà exploité sur le serveur (Restic, Borg ou équivalent) vers
un stockage hors hôte, chiffré, immuable si possible et supervisé. Conserver le
fichier `.env.production` dans un coffre séparé ; il permet de recréer les rôles et
de déchiffrer les sessions, mais ne doit pas être joint aux archives ordinaires.

Exemple à adapter seulement si Restic est déjà l'outil retenu et configuré sur le
serveur :

```bash
sudo restic backup /var/backups/ltnm-reservations --tag resa-ltnfm
sudo restic check
```

Brancher cette réplication au système de sauvegarde existant avec alerte d'échec. Le
timer livré ne doit pas être considéré comme complet tant qu'une copie hors hôte
chiffrée n'est pas confirmée.

Tester périodiquement la restauration sur une instance isolée. Le script fourni
vérifie le manifeste, conserve les médias courants dans un répertoire de secours,
recrée la base sous le rôle applicatif, restaure, migre et vide le cache. Il exige une
confirmation explicite :

```bash
cd /opt/resa-ltnfm
sudo ./deploy/debian/backup.sh
# Attendre et vérifier la copie hors hôte de cet instantané, puis seulement :
sudo CONFIRM_RESTORE=resa-ltnfm.agrobio-bretagne.org \
  ./deploy/debian/restore.sh \
  /var/backups/ltnm-reservations/ltnm-HORODATAGE
```

Le script exécute précisément les étapes suivantes :

1. vérifier le manifeste SHA-256 et la structure de l'archive média ;
2. prendre le verrou d'exploitation, démarrer les dépendances et arrêter `web` ;
3. déplacer les médias courants vers un répertoire de secours puis extraire l'archive ;
4. recréer la base appartenant à `POSTGRES_APP_USER` et restaurer le dump sous ce rôle ;
5. appliquer les migrations, vider Redis et redémarrer la pile avec healthchecks ;
6. conserver les anciens médias jusqu'à la vérification manuelle de `/readyz/` et d'un
   dossier réel.

Une restauration écrase des données et peut perdre les écritures postérieures à la
sauvegarde : ne pas l'automatiser et la faire valider par l'administrateur du serveur.

## 9. Mise à jour et retour arrière

Avant chaque mise à jour :

```bash
cd /opt/resa-ltnfm
sudo ./deploy/debian/backup.sh
# Vérifier la copie hors hôte, mettre le checkout sur la version voulue,
# attribuer un nouvel APP_RELEASE dans .env.production, puis :
sudo ./deploy/debian/deploy.sh
```

Conserver le tag Git et l'image `ltnm-reservations:APP_RELEASE` déployés ; ne pas
élaguer l'image précédente avant validation. Si aucune migration incompatible n'a été
appliquée, remettre le checkout et `APP_RELEASE` précédents puis recréer `web` sans
rebuild. Après une migration de schéma non rétrocompatible, privilégier une correction
en avant. Un retour arrière
complet exige la restauration coordonnée base + médias et perd les nouvelles
écritures ; il doit donc rester une opération de maintenance explicite.

```bash
sudo docker compose --env-file .env.production -f compose.production.yaml \
  up --detach --no-build --wait web
```

## 10. Points d'exploitation

- Les logs Docker sont limités à cinq fichiers de 10 Mio par service.
- Les limites CPU/mémoire de `.env.production` sont des valeurs initiales à ajuster
  après observation, sans empiéter sur les autres outils du serveur.
- Surveiller au minimum les conteneurs, `/healthz/`, la readiness interne, le
  certificat, les échecs SMTP,
  les timers systemd, l'espace disque et la réplication des sauvegardes.
- `var/emails/` est réservé au développement et peut contenir des liens sensibles ;
  ne jamais l'archiver ni le copier sur le serveur de production.
- Le publipostage est encore synchrone. Le timeout Gunicorn à 300 secondes limite les
  interruptions, mais une file de tâches idempotente est nécessaire avant une grosse
  campagne ; augmenter encore le timeout ne remplace pas cette évolution.
- Configurer SPF, DKIM et DMARC pour le domaine d'envoi, puis tester une livraison
  réelle avant l'ouverture aux salariés.
