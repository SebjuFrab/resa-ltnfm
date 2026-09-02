# Réservations scolaires — La Terre est Notre Métier

Application Django interne utilisée par l’équipe de la FRAB pour inscrire les groupes scolaires aux animations du salon **La Terre est Notre Métier**.

Le parcours principal n’est plus un formulaire rempli par le professeur. Un salarié connecté saisit l’inscription pendant qu’il échange avec le professeur par téléphone, choisit les animations du groupe, puis confirme le dossier. Le professeur reçoit automatiquement un récapitulatif par courriel.

L’édition configurée par défaut se déroule les **23 et 24 septembre 2026**. Les opérations réalisées par un salarié restent possibles jusqu’au salon. Les dates sont configurables par variables d’environnement.

## Parcours métier actuel

1. Le salarié se connecte avec un compte Django `is_staff` disposant des permissions nécessaires.
2. Depuis `/operations/groupes/nouveau/`, il saisit les informations communiquées par le professeur.
3. L’application propose un code de groupe unique composé de noms d’aliments, par exemple `truffe-pomme-doree`. Le salarié peut demander une autre proposition ou saisir un autre code disponible.
4. Le salarié filtre les séances du jour puis indique, pour chaque animation, le nombre d’étudiants et d’accompagnateurs présents. Un raccourci permet d’affecter tout le groupe.
5. Après vérification, il confirme l’inscription. Le professeur reçoit un courriel récapitulatif en texte et HTML.
6. Le salarié peut ensuite modifier les coordonnées, l’effectif ou le planning. Une inscription confirmée modifiée déclenche un nouveau courriel récapitulatif au professeur.
7. Avant le salon, l’équipe peut préparer un publipostage final. Chaque professeur reçoit les informations générales avec le récapitulatif de son groupe ; chaque responsable d’animation reçoit une synthèse des groupes attendus.

L’accueil `/` redirige vers le tableau de bord authentifié `/operations/`. L’ancien parcours public est **désactivé par défaut**. Il ne peut être réactivé temporairement sous `/ancien-parcours/` qu’avec `ENABLE_LEGACY_PUBLIC_FLOW=1`, par exemple pendant une reprise contrôlée d’anciens liens.

## Données d’un groupe

La fiche interne contient :

- nom et prénom de l’enseignant ;
- courriel et téléphone de l’enseignant ;
- établissement existant ou nouvel établissement, commune et département ;
- code de groupe unique fondé sur un aliment ;
- famille de groupe paramétrable ;
- niveau scolaire et remarque libre sur le niveau ;
- nombre d’étudiants ;
- nombre de professeurs ou accompagnateurs ;
- remarque générale ;
- jour de visite.

L’**effectif total** n’est pas ressaisi : il est calculé comme `étudiants + professeurs/accompagnateurs`. Les familles de groupe et les niveaux scolaires se gèrent dans l’administration Django et peuvent être activés, désactivés et ordonnés.

Les paramètres initiaux couvrent les lycées agricoles, professionnels, généraux et technologiques, l’enseignement supérieur et les groupes mixtes. Les niveaux proposés vont du CAP/CAP agricole au master ou cursus ingénieur. La saisie d’un établissement propose les quatre départements bretons (`22`, `29`, `35`, `56`) et les départements directement limitrophes (`44`, `49`, `50`, `53`).

## Règles fonctionnelles

| Sujet | Règle appliquée |
| --- | --- |
| Accès | Le parcours courant est réservé aux salariés authentifiés et contrôlé par les permissions Django. |
| Capacité | Les étudiants **et** les professeurs/accompagnateurs consomment la jauge de chaque séance. |
| Affectation | Chaque séance peut recevoir tout le groupe ou seulement une partie, avec des effectifs étudiants et accompagnateurs distincts. Zéro étudiant retire la réservation. |
| Planning | Les périodes sans animation sont autorisées. Sur des séances qui se chevauchent, la somme des étudiants et celle des accompagnateurs ne peuvent pas dépasser les effectifs du groupe. |
| Brouillon | Une inscription en cours immobilise ses places pendant 60 minutes. L’enregistrement du planning renouvelle ce délai. |
| Identifiants | L’UUID technique reste non séquentiel. Le code alimentaire est l’identifiant court, lisible et unique du groupe. |
| Courriels | Les envois transactionnels sont programmés après validation de la transaction. Un échec SMTP n’annule pas l’inscription et reste journalisé. |
| Modification | La modification d’une inscription confirmée ou de son planning envoie un récapitulatif de modification au professeur. |
| Annulation | L’annulation libère immédiatement les places et prévient le professeur. |

La capacité restante n’est pas stockée dans un compteur faisant autorité. Elle est recalculée depuis les réservations actives des inscriptions confirmées et des brouillons dont la retenue est encore valide. Toute écriture critique verrouille les séances concernées dans un ordre stable avec `transaction.atomic()` et `select_for_update()` puis revérifie la jauge.

**PostgreSQL est obligatoire en production et pour garantir le verrouillage concurrent.** SQLite convient au développement léger et aux tests non concurrents.

## Interface opérationnelle

### Tableau de bord

La page `/operations/` affiche notamment :

- le nombre d’établissements et de groupes confirmés ;
- les étudiants, accompagnateurs et participants par jour ;
- les groupes confirmés sans séance ;
- les capacités réservées et restantes ;
- les séances complètes, peu remplies ou en surcapacité ;
- les brouillons ;
- une recherche paginée par référence, code de groupe, établissement, professeur, courriel, date et statut ;
- les accès à la création d’un groupe, aux animations, à l’import, aux exports et au publipostage.

### Consultation et filtres des animations

La page `/operations/animations/` présente les séances et leur disponibilité. Les filtres disponibles sont :

- recherche dans le titre, la description, le lieu, le responsable ou son courriel ;
- jour ;
- heure minimale et heure maximale ;
- statut de la séance ;
- séances ayant encore des places uniquement.

Sur cette page et lors du choix du planning d’un groupe, les résultats se mettent à jour automatiquement : immédiatement pour les listes, horaires et cases, et après une courte pause pour la recherche. Aucun bouton d’application n’est nécessaire. Le planning reprend les mêmes filtres, sauf le jour qui est déjà fixé par le groupe.

Après chaque filtrage, la page restaure sa position de défilement. Sur le planning d’un groupe, les effectifs saisis mais pas encore enregistrés sont conservés temporairement dans la session du navigateur et fusionnés lors de l’enregistrement final, y compris lorsqu’une animation est masquée par le filtre.

### Création et modification d’une inscription

La création interne suit trois écrans : informations du groupe, choix des animations, puis vérification et confirmation. La fiche finale permet ensuite :

- de consulter le groupe et ses réservations ;
- de modifier ses informations ;
- de modifier son programme ;
- d’annuler l’inscription ;
- de renvoyer le récapitulatif de confirmation ;
- de consulter les derniers journaux de courriels.

Si le jour de visite est modifié, les nouvelles informations restent temporairement dans la session du salarié. L’ancienne date et l’ancien planning ne sont remplacés qu’au moment où au moins une séance du nouveau jour est enregistrée. Abandonner cette étape laisse donc l’inscription confirmée intacte.

### Publipostage final

La page `/operations/publipostage/` permet de :

- filtrer les inscriptions confirmées par jour et/ou famille ;
- prévisualiser le nombre de professeurs et de responsables concernés ;
- signaler les adresses manquantes et exiger une confirmation explicite avant un envoi incomplet ;
- saisir un objet et un message général dans un éditeur enrichi ;
- insérer dans ce message les variables `{{ prenom }}`, `{{ nom }}`, `{{ programme }}` et `{{ nombre_inscrits }}` ;
- envoyer un message personnalisé à chaque professeur ;
- envoyer à chaque adresse de responsable une synthèse consolidée de ses séances et des groupes attendus ;
- consulter ensuite le statut détaillé de chaque livraison.

Le HTML saisi est nettoyé côté serveur par liste blanche. Le publipostage accepte les paragraphes, titres simples, listes, citations, gras, italique, soulignement et liens `http`, `https` ou `mailto`. Les variables sont remplacées à partir du contexte figé de chaque destinataire et toute variable inconnue bloque l’envoi. `nombre_inscrits` représente l’effectif total, accompagnateurs compris ; pour un responsable, il additionne les effectifs de ses séances et `prenom` reste vide. Le récapitulatif détaillé est toujours ajouté automatiquement sous le message personnalisé. Sans filtre de jour, seuls les jours configurés dans `EVENT_DATES` sont inclus et les dossiers anonymisés sont toujours exclus. Le système génère aussi une version texte et protège l’envoi contre une double soumission grâce à une clé d’idempotence.

Pour qu’un responsable reçoive le publipostage, son courriel doit être renseigné sur la séance, directement ou via l’import CSV.

## Catalogue et administration Django

L’administration `/admin/` permet, selon les permissions accordées, de gérer :

- les thématiques, niveaux scolaires et familles de groupe ;
- les animations, leur catégorie `Salle` ou `Extérieur`, leurs thématiques, descriptions, niveaux conseillés, consignes, accessibilité et image ;
- les séances, horaires, lieux, jauges, responsables, courriels des responsables et statuts `OPEN`, `CLOSED` ou `CANCELLED` ;
- les établissements et professeurs ;
- la consultation des inscriptions, réservations et événements d’audit ;
- les journaux de courriels transactionnels ;
- les campagnes et livraisons de publipostage.

Les modifications métier d’une inscription doivent passer par l’interface opérationnelle, qui applique les services transactionnels et les contrôles de capacité.

Lors de la migration, les anciennes catégories thématiques sont recopiées dans les thématiques des animations. La catégorie `Salle` ou `Extérieur` n’est jamais déduite automatiquement : elle doit être renseignée sur chaque animation historique depuis l’administration ou par le nouvel import CSV.

## Import CSV des groupes

L’import des groupes se trouve sous `/operations/import/groupes/`. Il affiche d’abord un aperçu complet et les erreurs de chaque ligne, sans écrire en base. La confirmation revalide ensuite le contenu et crée tout le fichier dans une transaction unique. Un modèle CSV UTF-8 prêt à compléter est disponible sur la page ou directement sous `/operations/import/groupes/modele.csv`.

```csv
nom_enseignant;prenom_enseignant;email_enseignant;telephone_enseignant;etablissement;type_etablissement;commune;departement;code_groupe;famille;niveau;jour;nb_etudiants;nb_accompagnateurs;effectif_total;remarque_niveau;remarque_generale
```

Toutes les colonnes sont obligatoires sauf `remarque_niveau` et `remarque_generale`. Le `code_groupe` doit être unique et ne peut pas être vide : il rend l’import rejouable sans recréer silencieusement les mêmes groupes. `effectif_total` doit être exactement égal à `nb_etudiants + nb_accompagnateurs` et sert de contrôle de cohérence.

- `type_etablissement` accepte un code Django tel que `AGRICULTURAL`, `HIGH_SCHOOL`, `HIGHER_EDUCATION` ou `OTHER`, ainsi que son libellé français ;
- `famille` accepte le slug ou le nom d’une famille active ;
- `niveau` accepte le code ou le libellé d’un niveau actif ;
- `jour` accepte le nom d’un jour du salon ou une date configurée ;
- le département doit appartenir à la liste proposée par l’application ;
- les établissements et enseignants strictement identiques sont réutilisés, sans écraser silencieusement une fiche existante.

Les groupes importés sont des **brouillons sans animation**. Ils ne consomment aucune jauge et aucun courriel n’est envoyé pendant l’import. Le salarié ouvre ensuite chaque groupe, choisit ses animations et utilise la confirmation habituelle ; les contrôles de capacité et l’envoi du récapitulatif s’appliquent alors normalement.

## Import CSV des animations et séances

L’import en deux étapes se trouve sous `/operations/import/seances/`. La première étape affiche un aperçu sans écrire en base ; la confirmation réexamine les données et effectue un import atomique. Un bouton sur cette page télécharge un modèle CSV UTF-8 prêt à compléter ; son adresse directe est `/operations/import/seances/modele.csv`.

### Colonnes canoniques

```csv
titre_animation;categorie;thematiques;lieu_de_rendez_vous;duree;jauge;jour;horaires;responsable;email_responsable
```

Les huit premières colonnes sont obligatoires. `categorie` vaut `Salle` ou `Extérieur`. `thematiques` contient une ou plusieurs thématiques actives séparées par `|`. `responsable` et `email_responsable` sont facultatives pour l’import, mais le courriel est nécessaire au publipostage destiné aux responsables.

Exemple :

```csv
titre_animation;categorie;thematiques;lieu_de_rendez_vous;duree;jauge;jour;horaires;responsable;email_responsable
Du blé au pain;Salle;Techniques végétales|Témoignage;Accueil Hall A;1h;30;mercredi;09:00,10:30,14:00;Marie Martin;marie@example.org
Haies et biodiversité;Extérieur;Biodiversité|Jeu de piste;Pôle bocage;45 min;25;24/09/2026;09h30,11h00;Jean Dupont;jean@example.org
```

Une ligne peut contenir plusieurs heures de début séparées par des virgules. Elle crée alors une séance par horaire, avec la même animation, la même durée, le même lieu, la même jauge et le même responsable. Dans un fichier dont le séparateur principal est la virgule, la cellule contenant plusieurs horaires doit être placée entre guillemets.

### Règles de lecture

- fichier `.csv`, encodé en UTF-8 avec ou sans BOM, ou en Windows-1252 ;
- séparateur point-virgule ou virgule détecté depuis l’en-tête ;
- 500 lignes source maximum et 2 000 séances générées maximum ;
- noms de colonnes normalisés sans tenir compte des accents, espaces, tirets ou casse ;
- `categorie` accepte `Salle` ou `Extérieur`, sans tenir compte des accents ni de la casse ;
- `thematiques` exige au moins une thématique active existante ; plusieurs valeurs sont séparées par `|` ;
- `duree` accepte un nombre de minutes, `45 min`, `1h`, `1h30` ou `01:30` ;
- `jauge` doit être un entier strictement positif ;
- `jour` accepte un jour du salon (`mercredi`, `jeudi`) ou une date autorisée au format `AAAA-MM-JJ` ou `JJ/MM/AAAA` ;
- `horaires` accepte des heures `HH:MM`, `HH:MM:SS` ou `9h30`, séparées par des virgules ;
- les horaires d’une même ligne ne peuvent pas se chevaucher compte tenu de la durée ;
- `email_responsable`, lorsqu’il est fourni, doit être une adresse valide ;
- les doublons sont refusés ;
- une animation existante de même titre et durée est réutilisée ; sa catégorie et ses thématiques sont mises à jour atomiquement avec les séances ;
- une animation absente est créée avec la catégorie et les thématiques du fichier ;
- les séances créées sont ouvertes par défaut.

Si une donnée devient invalide entre l’aperçu et la confirmation, aucune animation ni séance du fichier n’est créée.

## Exports CSV

Les trois exports sont encodés en UTF-8 avec BOM et utilisent des fins de ligne CRLF. Le séparateur est le point-virgule par défaut, avec une option virgule. Les cellules ressemblant à une formule de tableur sont protégées.

- `inscriptions.csv` contient les coordonnées, le code groupe, la famille, la commune, le département, le niveau, les remarques, les effectifs étudiants/accompagnateurs et l’effectif total.
- `reservations.csv` contient une ligne par réservation confirmée, avec les données du groupe et son effectif total.
- `seances.csv` contient la synthèse des séances, les groupes confirmés, le total réservé, la capacité maximale et la capacité restante.

Les capacités incluent les étudiants et les accompagnateurs, ainsi que les retenues de brouillons encore actives.

## Routes principales

| Route | Accès | Rôle |
| --- | --- | --- |
| `/` | redirection | redirection vers `/operations/` |
| `/admin/login/` | public, limité en débit | connexion d’un salarié |
| `/operations/` | staff + permissions de lecture métier | tableau de bord et recherche |
| `/operations/animations/` | staff + `catalogue.view_session` | liste filtrable des animations et séances |
| `/operations/groupes/nouveau/` | staff + droits complets du parcours interne | saisie téléphonique d’un groupe |
| `/operations/groupes/code/aleatoire/` | staff + `inscriptions.add_registration` | suggestion d’un code alimentaire unique |
| `/operations/groupes/<uuid>/` | staff + lecture inscription/contact/réservations/courriels | fiche d’une inscription |
| `/operations/groupes/<uuid>/animations/` | staff + droits complets du parcours interne | choix ou modification des animations |
| `/operations/groupes/<uuid>/verifier/` | staff + droits complets du parcours interne | vérification et confirmation |
| `/operations/groupes/<uuid>/modifier/` | staff + droits complets du parcours interne | modification des informations |
| `/operations/groupes/<uuid>/annuler/` | staff + droits complets du parcours interne | annulation |
| `/operations/groupes/<uuid>/renvoyer/` | staff + droits complets du parcours interne | renvoi du récapitulatif |
| `/operations/publipostage/` | staff + `communication.send_mailing` | aperçu et envoi final |
| `/operations/publipostage/<id>/` | staff + `communication.send_mailing` | détail d’une campagne |
| `/operations/import/groupes/` | staff + droits complets du parcours interne | aperçu et import atomique des groupes |
| `/operations/import/groupes/modele.csv` | staff + droits complets du parcours interne | modèle CSV des groupes |
| `/operations/import/seances/` | staff + ajout/modification animation et ajout séance | import CSV en deux étapes |
| `/operations/import/seances/modele.csv` | staff + ajout/modification animation et ajout séance | modèle CSV téléchargeable |
| `/operations/exports/telecharger/` | staff + permissions des données exportées | téléchargement d’un export choisi |
| `/operations/exports/inscriptions.csv` | staff + lecture des données personnelles | export direct des inscriptions |
| `/operations/exports/reservations.csv` | staff + permissions de lecture composées | export direct des réservations |
| `/operations/exports/seances.csv` | staff + permissions de lecture composées | export direct des séances |
| `/admin/` | staff + permissions Django | paramétrage et administration |
| `/ancien-parcours/` | désactivé par défaut | ancien formulaire professeur, uniquement si `ENABLE_LEGACY_PUBLIC_FLOW=1` |

Lorsque la compatibilité est explicitement activée, toutes les anciennes routes d’inscription, de planning et de gestion par lien sont préfixées par `/ancien-parcours/`.

## Architecture

Le projet est un monolithe Django rendu côté serveur, sans API séparée :

- Python 3.12 ou supérieur ;
- Django 5.2 LTS ;
- PostgreSQL via psycopg 3, avec repli SQLite en développement ;
- Redis avec `django-redis` pour les limites de débit partagées ;
- gabarits Django, HTML et CSS local sans framework JavaScript ;
- Pillow pour les images, WhiteNoise pour les statiques et Gunicorn en production ;
- pytest, pytest-django, pytest-cov et ruff pour les tests et la qualité.

```text
catalogue/                    catégories, niveaux, animations et séances
communication/                courriels transactionnels, publipostages et journaux
config/settings/              configurations développement, test et production
inscriptions/
  codes.py                    génération et normalisation des codes alimentaires
  services/capacity.py        calcul et verrouillage des capacités
  services/registration.py    cycle de vie transactionnel des inscriptions
  models.py                   groupes, familles, établissements, professeurs et audit
operations/
  group_imports.py            aperçu et import atomique des groupes
  imports.py                  aperçu et import atomique des animations/séances
  exports.py                  exports CSV
  forms.py, views.py, urls.py parcours interne FRAB
templates/                    gabarit HTML commun
static/css/app.css            charte graphique et styles d’impression
inscriptions/management/commands/
  anonymize_old_registrations.py anonymisation selon la date de visite
compose.yaml                  développement PostgreSQL + Redis
compose.production.yaml       Gunicorn + PostgreSQL + Redis
Dockerfile                    image de l’application
requirements.txt              dépendances d’exécution, tests et qualité
pyproject.toml                paquet Python et configuration des outils
```

Principales relations :

```text
Theme >──< Animation >──< SchoolLevel
             │  └── catégorie Salle ou Extérieur
             │
             └──< Session <── Reservation >── Registration
                                              ├── GroupFamily
                                              ├── Institution
                                              ├── Teacher
                                              ├── SchoolLevel
                                              ├── RegistrationEvent
                                              └── EmailLog

MailingCampaign ──< MailingDelivery
```

## Installation locale sous PowerShell

Depuis la racine du dépôt :

```powershell
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_demo
python manage.py runserver 8001
```

Ouvrir ensuite :

- interface interne : <http://127.0.0.1:8001/operations/> ;
- administration : <http://127.0.0.1:8001/admin/>.

Le port `8001` évite un conflit si un autre service utilise déjà `8000`. Tout autre port libre peut être choisi avec `python manage.py runserver PORT`.

Sans `POSTGRES_HOST`, les réglages de développement utilisent `db.sqlite3`. Le fichier `.env` n’est pas chargé automatiquement par `manage.py` ; il est lu par Docker Compose. Pour une configuration locale, définir les variables dans le terminal avant de démarrer Django :

```powershell
$env:ORGANIZATION_EMAIL = "scolaires@example.org"
$env:ORGANIZATION_PHONE = "02 00 00 00 00"
$env:EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
$env:EMAIL_FILE_PATH = "var/emails"
python manage.py runserver 8001
```

Le backend de développement écrit les messages dans `var/emails/`. Ces fichiers contiennent le corps des courriels et parfois des liens sensibles : ne jamais les archiver, les partager ou les copier en production. Configurer SMTP pour des envois réels.

## Installation avec Docker Compose

Le fichier `compose.yaml` démarre PostgreSQL, Redis et le serveur Django de développement :

```powershell
Copy-Item .env.example .env
# Remplacer les secrets et mots de passe dans .env.
docker compose up --build -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_demo
```

L’application est alors disponible sur <http://localhost:8000/>. Les données PostgreSQL restent dans le volume logique `postgres_data` après `docker compose down`; son nom Docker réel est préfixé par le projet Compose. En production il s'agit par défaut de `ltnm_resa_postgres_data`.

Commandes utiles :

```powershell
docker compose logs -f web
docker compose exec web python manage.py shell
docker compose exec web python manage.py showmigrations
docker compose restart web
docker compose down
```

Ne pas utiliser `docker compose down -v` sans sauvegarde : `-v` supprime le volume PostgreSQL.

## Variables d’environnement principales

| Variable | Défaut ou exemple | Utilisation |
| --- | --- | --- |
| `DJANGO_SETTINGS_MODULE` | `config.settings.development` | réglages actifs |
| `DJANGO_SECRET_KEY` | valeur de développement | signature Django ; secret long et stable obligatoire en production |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | hôtes autorisés |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `http://localhost:8000` | origines complètes autorisées |
| `POSTGRES_HOST` | vide en local, `db` avec Compose | active PostgreSQL lorsqu’il est renseigné |
| `POSTGRES_DB` | `ltnm` | base PostgreSQL |
| `POSTGRES_USER` | `ltnm` | utilisateur PostgreSQL |
| `POSTGRES_PASSWORD` | à définir | mot de passe PostgreSQL |
| `POSTGRES_PORT` | `5432` | port PostgreSQL |
| `REDIS_URL` | `redis://redis:6379/0` | cache partagé et limitation de débit |
| `EMAIL_BACKEND` | backend fichier en développement | transport des courriels |
| `EMAIL_FILE_PATH` | `var/emails` | boîte de développement locale |
| `EMAIL_HOST`, `EMAIL_PORT` | `localhost`, `1025` | serveur SMTP |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | vides | authentification SMTP |
| `EMAIL_USE_TLS`, `EMAIL_USE_SSL` | `0`, `0` | chiffrement SMTP ; n’en activer qu’un |
| `EMAIL_TIMEOUT` | `10` | délai maximal d’un envoi synchrone |
| `DEFAULT_FROM_EMAIL` | `inscriptions@example.test` | expéditeur des messages |
| `ORGANIZATION_EMAIL` | `contact@example.test` | contact de l’organisation |
| `ORGANIZATION_PHONE` | vide | téléphone de contact |
| `EVENT_DATES` | `2026-09-23,2026-09-24` | jours autorisés pour les inscriptions et imports |
| `REGISTRATION_EDIT_DEADLINE` | `2026-09-16T23:59:00+02:00` | clôture du parcours professeur historique ; les actions staff ne sont pas bloquées |
| `DRAFT_HOLD_MINUTES` | `60` | durée de retenue des places d’un brouillon public (les brouillons enregistrés par l’équipe n’expirent pas) |
| `DATA_RETENTION_DAYS` | `730` | délai avant anonymisation |
| `ENABLE_LEGACY_PUBLIC_FLOW` | `0` | réactive temporairement l’ancien parcours public sous `/ancien-parcours/` |
| `TRUST_PROXY_HEADERS` | `0` | prise en compte des en-têtes du proxy de confiance |

Les booléens acceptent notamment `1`, `true`, `yes` ou `on`. Les listes, comme `EVENT_DATES`, utilisent des valeurs séparées par des virgules.

## Tests et contrôles

Avec SQLite :

```powershell
$env:POSTGRES_HOST = ""
python -m pytest
python -m ruff check .
python manage.py check
python manage.py makemigrations --check --dry-run
```

Le test de concurrence est ignoré sous SQLite, qui ne fournit pas le verrouillage de lignes attendu. Pour le lancer réellement, utiliser une base PostgreSQL de test et définir les variables `POSTGRES_*` :

```powershell
$env:DJANGO_SETTINGS_MODULE = "config.settings.test"
$env:POSTGRES_HOST = "127.0.0.1"
$env:POSTGRES_PORT = "5432"
$env:POSTGRES_DB = "ltnm"
$env:POSTGRES_USER = "ltnm"
$env:POSTGRES_PASSWORD = "mot-de-passe-local"
python -m pytest
```

La suite couvre notamment les modèles, la génération des codes, les familles, les capacités incluant les accompagnateurs, les services transactionnels, le parcours interne, les imports atomiques des groupes et des animations, les exports, les courriels et le publipostage.

## Mise en production

`compose.yaml` utilise `runserver` et vise le développement. Pour Debian, la pile de production isole Gunicorn, PostgreSQL et Redis sous le projet Compose `ltnm_resa`; elle ne publie Gunicorn que sur `127.0.0.1:18001` afin de cohabiter avec les autres services du serveur. Les migrations et la collecte des statiques sont exécutées par une étape de release séparée du démarrage web.

La procédure complète et directement adaptée à `https://resa-ltnfm.agrobio-bretagne.org` se trouve dans [deploy/README.md](deploy/README.md). Elle couvre l'installation Docker sur Debian, Nginx/HTTPS, les secrets, les healthchecks, systemd, les sauvegardes, l'anonymisation, les mises à jour et le retour arrière.

Avant le déploiement :

1. utiliser `config.settings.production`, PostgreSQL et Redis ;
2. fournir une clé Django longue, secrète et stable ;
3. configurer les hôtes, les origines CSRF et HTTPS ;
4. appliquer les migrations ;
5. collecter les fichiers statiques ;
6. configurer et tester SMTP ;
7. rendre le stockage des médias persistant ;
8. sauvegarder PostgreSQL et les médias, puis tester une restauration ;
9. accorder aux comptes salariés uniquement les permissions utiles.

Configuration SMTP typique :

```dotenv
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.example.org
EMAIL_PORT=587
EMAIL_HOST_USER=compte-smtp
EMAIL_HOST_PASSWORD=secret
EMAIL_USE_TLS=1
EMAIL_TIMEOUT=10
DEFAULT_FROM_EMAIL=inscriptions@example.org
ORGANIZATION_EMAIL=scolaires@example.org
```

Les envois transactionnels produisent un `EmailLog`. Les publipostages produisent une `MailingCampaign` et une `MailingDelivery` par destinataire, avec le statut et l’erreur éventuelle. Les messages restent synchrones après validation de la transaction : une file de tâches asynchrone n’est pas fournie.

## Sécurité et RGPD

- Aucun nom ni identifiant individuel d’étudiant n’est collecté.
- Les formulaires d’écriture utilisent POST et la protection CSRF.
- Le parcours opérationnel, les exports et le publipostage exigent un compte staff et des permissions explicites.
- Le HTML du publipostage est nettoyé par une liste blanche avant envoi.
- Les erreurs SMTP enregistrées sont nettoyées des adresses et secrets connus.
- Les événements métier sont consultables en lecture seule dans l’administration.
- `python manage.py anonymize_old_registrations` anonymise les visites plus anciennes que `DATA_RETENTION_DAYS` ; `--dry-run` permet un contrôle et `--before AAAA-MM-JJ` impose une date.
- Les sauvegardes contiennent des coordonnées de professeurs : elles doivent être chiffrées, protégées et soumises à une politique de rétention.

Les champs de remarque sont libres. Les consignes et les droits accordés au personnel doivent éviter toute saisie de données personnelles ou de santé non nécessaires.

## Dépannage

### `No module named django`, `pytest` ou `ruff`

Vérifier l’environnement virtuel puis réinstaller les dépendances :

```powershell
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### Le port est déjà utilisé

Lancer Django sur un autre port :

```powershell
python manage.py runserver 8001
```

### L’application utilise SQLite au lieu de PostgreSQL

PostgreSQL n’est activé que si `POSTGRES_HOST` est défini et non vide :

```powershell
python -c "import os; print(os.environ.get('POSTGRES_HOST'))"
```

Un fichier `.env` local n’est pas chargé par `manage.py`. Avec Compose, vérifier qu’il existe avant `docker compose up`.

### Un courriel n’a pas été reçu

Une panne SMTP n’annule pas l’inscription. Consulter les journaux sur la fiche du groupe ou dans l’administration. En développement, vérifier les fichiers sous `var/emails/` ; en production, contrôler la configuration SMTP.

### Le publipostage n’inclut pas un responsable

Vérifier que la séance possède une adresse `email_responsable` valide et qu’au moins un groupe confirmé correspondant aux filtres est réservé sur cette séance.

### L’import est refusé

Vérifier les huit colonnes obligatoires, la catégorie `Salle` ou `Extérieur`, les thématiques actives séparées par `|`, l’encodage, le séparateur, les jours autorisés, les formats de durée et d’heure, la jauge positive, les doublons et le courriel du responsable. La page d’aperçu affiche les erreurs avec leur numéro de ligne.

### Des places semblent occupées sans groupe confirmé

Un brouillon actif peut retenir les places de tous ses participants pendant 60 minutes. Il cesse automatiquement de compter après son expiration.

## Limites connues

- pas d’export XLSX ;
- pas de relance automatique des envois SMTP en échec ;
- pas de traitement automatique des groupes lorsqu’une séance est annulée ;
- pas d’ordonnanceur dans Django ; le déploiement Debian fournit un timer systemd pour l’anonymisation ;
- pas de file de tâches asynchrone ;
- pas d’API publique ni d’application JavaScript séparée ;
- pas de suivi nominatif des étudiants.

## Documentation complémentaire

Le document [docs/conception-mvp.md](docs/conception-mvp.md) conserve l’analyse initiale. En cas d’écart, le code et le présent README décrivent le comportement livré.
