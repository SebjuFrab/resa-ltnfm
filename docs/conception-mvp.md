# Conception fonctionnelle et technique du MVP

## 1. Périmètre et hypothèses

Le salon a lieu les **23 et 24 septembre 2026**. Une inscription représente un groupe scolaire, son professeur référent et un seul jour de visite. Les élèves ne sont jamais identifiés. Le nombre d’élèves affecté aux séances est manipulé sous forme d’effectifs.

Dans le MVP, une « plage horaire » n’est pas une donnée indépendante : les incohérences sont détectées à partir du chevauchement réel des intervalles `[début, fin)`. Deux séances sont compatibles lorsque la fin de l’une est égale au début de l’autre. Les périodes sans animation sont autorisées et ne produisent aucune alerte.

## 2. Modèle de données Django proposé

### `catalogue.Category`

- `name: CharField(100, unique=True)`
- `slug: SlugField(unique=True)`
- `is_active: BooleanField(default=True)`

### `catalogue.Animation`

- `title: CharField(200)`
- `slug: SlugField(220, unique=True)`
- `short_description: CharField(300)`
- `description: TextField(blank=True)`
- `category: ForeignKey(Category, PROTECT)`
- `recommended_levels: ManyToManyField(SchoolLevel, blank=True)`
- `indicative_duration: PositiveSmallIntegerField` — minutes
- `image: ImageField(blank=True)` — image facultative, prise en charge dans le MVP
- `instructions: TextField(blank=True)`
- `accessibility: TextField(blank=True)`
- `is_active: BooleanField(default=True)`
- `created_at`, `updated_at`

### `catalogue.SchoolLevel`

- `code: CharField(30, unique=True)`
- `label: CharField(100)`
- `sort_order: PositiveSmallIntegerField(default=0)`

Une table de référence évite les valeurs libres divergentes et sert aux filtres.

### `catalogue.Session`

- `animation: ForeignKey(Animation, PROTECT, related_name="sessions")`
- `date: DateField(db_index=True)`
- `starts_at: TimeField`
- `ends_at: TimeField`
- `location: CharField(200)`
- `max_capacity: PositiveIntegerField`
- `status: CharField(choices=OPEN/CLOSED/CANCELLED, db_index=True)`
- `organizer: CharField(200, blank=True)`
- `internal_comment: TextField(blank=True)`
- `created_at`, `updated_at`

Contraintes : `max_capacity > 0` et `ends_at > starts_at`. La capacité réservée n’est pas stockée ; elle est agrégée depuis les réservations actives. Une propriété annotée/service expose `reserved_capacity` et `remaining_capacity`.

### `inscriptions.Institution`

- `name: CharField(200, db_index=True)`
- `institution_type: CharField(choices=...)`
- `address: CharField(255)`
- `postal_code: CharField(10, db_index=True)`
- `city: CharField(120, db_index=True)`
- `department: CharField(3, db_index=True)`
- `phone: CharField(30, blank=True)`
- `administrative_email: EmailField(blank=True)`
- `created_at`, `updated_at`

Un index sur `(name, postal_code)` facilite la recherche. La fusion automatique de doublons est exclue du MVP.

### `inscriptions.Teacher`

- `institution: ForeignKey(Institution, PROTECT, related_name="teachers")`
- `first_name: CharField(100)`
- `last_name: CharField(100)`
- `email: EmailField(db_index=True)`
- `phone: CharField(30)`
- `created_at`, `updated_at`

Ce contact peut être réutilisé. Le MVP ne conserve pas d’instantané séparé : avant la modification d’un professeur partagé depuis un lien de gestion, il clone le contact afin de ne pas modifier les autres inscriptions.

### `inscriptions.Registration`

- `reference: UUIDField(default=uuid.uuid4, unique=True, editable=False)` — référence publique non séquentielle ; aucune numérotation métier
- `institution: ForeignKey(Institution, PROTECT, related_name="registrations")`
- `teacher: ForeignKey(Teacher, PROTECT, related_name="registrations")`
- `group_name: CharField(150)`
- `school_level: ForeignKey(SchoolLevel, PROTECT)`
- `student_count: PositiveSmallIntegerField`
- `chaperone_count: PositiveSmallIntegerField(default=0)`
- `visit_date: DateField(db_index=True)`
- `special_needs: TextField(blank=True)`
- `comment: TextField(blank=True)`
- `status: CharField(choices=DRAFT/CONFIRMED/CANCELLED, db_index=True)`
- `draft_expires_at: DateTimeField(null=True, blank=True, db_index=True)` — une heure après la dernière sauvegarde réservant des places
- `edit_token_digest: CharField(64, unique=True, editable=False)` — SHA-256/HMAC du jeton, jamais le jeton brut
- `token_created_at: DateTimeField`
- `token_revoked_at: DateTimeField(null=True, blank=True)`
- `confirmed_at`, `cancelled_at`, `created_at`, `updated_at`

Contraintes : `student_count > 0`, `chaperone_count >= 0`. Le jeton brut n’est présent que dans le lien envoyé. La date limite de modification est un paramètre global et peut être complétée ultérieurement par une date propre à l’inscription. Une référence UUID sert à la recherche et aux échanges avec l’organisation, sans compteur ni numéro annuel.

### `inscriptions.Reservation`

- `registration: ForeignKey(Registration, CASCADE, related_name="reservations")`
- `session: ForeignKey(Session, PROTECT, related_name="reservations")`
- `student_count: PositiveSmallIntegerField`
- `chaperone_count: PositiveSmallIntegerField(default=0)`
- `status: CharField(choices=ACTIVE/CANCELLED, db_index=True)`
- `cancelled_at: DateTimeField(null=True, blank=True)`
- `created_at`, `updated_at`

Contraintes : effectif élève strictement positif ; une seule réservation active par couple inscription/séance (contrainte unique conditionnelle PostgreSQL). Les accompagnateurs sont informatifs et ne consomment jamais la capacité.

### `inscriptions.RegistrationEvent`

- `registration: ForeignKey(Registration, PROTECT, related_name="events")`
- `event_type: CharField(choices=CREATED/CONFIRMED/UPDATED/CANCELLED/EMAIL_SENT/...)`
- `actor_kind: CharField(choices=TEACHER/STAFF/SYSTEM)`
- `actor_user: ForeignKey(settings.AUTH_USER_MODEL, SET_NULL, null=True, blank=True)`
- `changes: JSONField(default=dict)` — valeurs métier utiles, sans jeton
- `created_at: DateTimeField(auto_now_add=True)`

Ce journal est immuable via les instances et l’administration. La commande de rétention RGPD peut toutefois expurger les données personnelles de `changes` par mise à jour directe.

### `communication.EmailLog` (minimal)

- `registration: ForeignKey(Registration, SET_NULL, null=True)`
- `kind: CharField(choices=CONFIRMATION/MODIFICATION/CANCELLATION)`
- `recipient: EmailField`
- `status: CharField(choices=PENDING/SENT/FAILED)`
- `provider_message_id: CharField(blank=True)`
- `error_summary: TextField(blank=True)` — nettoyé de toute donnée sensible
- `created_at`, `sent_at`

Les modèles de messages sont des gabarits versionnés dans le MVP ; leur administration est reportée.

## 3. Règles de gestion

### Capacités et concurrence

1. Toute création, modification, réactivation ou déplacement d’une réservation s’exécute dans `transaction.atomic()`.
2. Les séances touchées sont verrouillées avec `select_for_update()` dans un ordre stable par identifiant pour éviter les interblocages.
3. La somme des réservations actives confirmées et des réservations de brouillons non expirés est recalculée en base sous verrou.
4. L’écriture est refusée si le nouvel effectif ferait dépasser `max_capacity`.
5. Une séance inactive, fermée, annulée, d’un autre jour ou une inscription annulée est refusée.
6. Le test concurrent utilise `TransactionTestCase`/pytest et PostgreSQL ; SQLite ne permet pas de valider ce comportement.

### Cohérence du programme

Pour chaque ensemble de séances qui se chevauchent, la somme des élèves affectés ne peut pas dépasser `Registration.student_count`. Un total inférieur est accepté sans avertissement : les trous dans le programme sont autorisés. Un total égal signifie que tous les élèves sont affectés pendant cet intervalle.

Le contrôle doit tenir compte de chevauchements partiels, pas seulement d’heures de début identiques. Lors d’une baisse de l’effectif du groupe, la modification est refusée tant que des réservations simultanées dépassent le nouvel effectif.

### Cycle de vie

- `DRAFT` : informations et choix modifiables, aucun courriel de confirmation. Les places sont maintenues pendant une heure après la dernière sauvegarde concernée ; après `draft_expires_at`, elles ne comptent plus dans la capacité et devront être revérifiées avant confirmation.
- `CONFIRMED` : inscription validée ; modification possible jusqu’à la date limite.
- `CANCELLED` : réservations actives annulées dans la même transaction, capacités immédiatement libérées.

La validation finale reverrouille toutes les séances réservées, contrôle capacités, jour, statut et cohérence, puis confirme l’inscription. Le courriel est déclenché avec `transaction.on_commit()` afin de ne jamais annoncer une transaction annulée. Une panne SMTP ne remet pas en cause la confirmation : l’échec est journalisé et l’envoi peut être retenté.

### Jeton sécurisé

Le jeton contient au moins 256 bits d’aléa (`secrets.token_urlsafe(32)` ou plus). Seule son empreinte est conservée. La comparaison est faite en temps constant. La révocation, l’annulation et la date limite rendent le lien inutilisable. Le token ne doit figurer ni dans les logs applicatifs ni dans les événements d’audit.

### Import CSV

L’import est réservé au personnel autorisé, avec aperçu et validation avant écriture. Les colonnes implémentées sont `animation`, `date`, `heure_debut`, `heure_fin`, `lieu`, `capacite`, `statut`, `organisateur`. Les lignes sont validées séparément et aucune écriture n’a lieu si le fichier contient une erreur : l’import est atomique.

### Exports MVP

Trois exports CSV protégés : inscriptions, réservations et synthèse par séance. Encodage UTF-8 avec BOM et séparateur configurable (point-virgule par défaut pour Excel francophone). Les exports n’incluent aucun jeton.

## 4. Principaux écrans

### Parcours professeur

1. **Accueil et établissement** : contexte, dates, sélection d’un établissement existant dans une liste ou saisie d’un nouvel établissement.
2. **Informations du groupe** : établissement, professeur, groupe, niveau, effectifs, jour et besoins.
3. **Planning du jour** : séances chronologiques, filtres, capacité restante et saisie des effectifs ; une séance pleine borne la saisie à zéro.
4. **Vérification** : récapitulatif du planning, erreurs bloquantes de capacité ou de chevauchement et bouton de confirmation ; les périodes libres ne sont pas signalées.
5. **Confirmation imprimable** : référence UUID, informations du groupe, planning, lieux et consignes. L’état technique d’envoi reste réservé au personnel.
6. **Gestion par lien** : récapitulatif, modification, annulation complète ; message de contact après échéance.

Le parcours est multi-étapes côté serveur. Le brouillon est identifié par session navigateur avant émission du lien ; aucune authentification professeur n’est requise.

### Administration et opérations

1. **Administration Django** : gestion des animations et séances ; inscriptions et réservations en lecture seule avec actions métier contrôlées, duplication d’animation et ouverture/fermeture/annulation.
2. **Import des séances** : dépôt CSV, aperçu, rapport d’erreurs, confirmation.
3. **Tableau de bord** : établissements, groupes, élèves par jour, remplissage global, séances pleines/faibles, inscriptions incomplètes.
4. **Recherche opérationnelle** : référence, établissement, professeur, courriel, date et statut.
5. **Exports** : choix du type, du jour et téléchargement CSV.

## 5. Arborescence livrée

```text
.
├── .env.example
├── .gitignore
├── compose.production.yaml
├── compose.yaml
├── Dockerfile
├── README.md
├── pyproject.toml
├── manage.py
├── config/
│   ├── settings/
│   │   ├── base.py
│   │   ├── development.py
│   │   └── production.py
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
├── catalogue/
│   ├── admin.py
│   ├── models.py
│   ├── services.py
│   ├── migrations/
│   └── tests/
├── inscriptions/
│   ├── admin.py
│   ├── models.py
│   ├── forms.py
│   ├── services/
│   │   ├── capacity.py
│   │   ├── registration.py
│   │   └── tokens.py
│   ├── urls.py
│   ├── views.py
│   ├── migrations/
│   └── tests/
├── communication/
│   ├── services.py
│   ├── models.py
│   ├── templates/emails/
│   └── tests/
├── operations/
│   ├── forms.py
│   ├── imports.py
│   ├── exports.py
│   ├── urls.py
│   ├── views.py
│   ├── management/commands/seed_demo.py
│   └── tests/
├── templates/base.html
├── static/
│   └── css/app.css
└── docs/
    └── conception-mvp.md
```

## 6. Étapes de réalisation livrées

1. Socle Docker/Django/PostgreSQL, configuration et contrôle de santé.
2. Modèles catalogue, administration, migrations et tests.
3. Modèles inscriptions, service transactionnel de capacité et tests anti-surbooking.
4. Parcours professeur jusqu’au brouillon et vérification du planning.
5. Confirmation, jeton de modification, courriels et tests d’accès.
6. Import CSV, tableau de bord et exports CSV.
7. Données de démonstration, tests de bout en bout, sécurité et documentation finale.

Chaque étape est couverte par les contrôles et tests décrits dans le README.

## 7. Décisions validées et points restant ouverts

### Décisions validées le 22 juillet 2026

1. La capacité compte uniquement les élèves ; les accompagnateurs sont informatifs.
2. Les trous dans le programme sont permis et ne génèrent aucune alerte.
3. Le salon se déroule les 23 et 24 septembre 2026.
4. Il n’y a aucune numérotation métier ; une référence UUID non séquentielle est utilisée.
5. Un brouillon immobilise les places pendant une heure après sa dernière sauvegarde.
6. En l’absence de préférence métier sur SMTP, le choix technique retenu est de confirmer l’inscription, journaliser l’échec et permettre un nouvel envoi.
7. La clôture des créations, confirmations et modifications est fixée une semaine avant le salon, soit le 16 septembre 2026 à 23 h 59 (heure de Paris).
8. Le délai d’une heure repart après chaque sauvegarde du planning.

### À valider avant l’ouverture publique

9. Faire valider par l’organisation les types d’établissement, niveaux scolaires et catégories officiels.
10. Choisir le fournisseur SMTP, renseigner ses secrets et tester la délivrabilité réelle.
11. Valider la politique de doublons des établissements ; le MVP n’effectue aucune fusion automatique.
12. Décider si une vérification de propriété du courriel ou un dispositif anti-robot doit compléter le parcours public.

## 8. Garanties techniques à préserver

- La vérification des effectifs traite les chevauchements partiels, et pas seulement les heures de début.
- Les listes annotent `remaining_capacity` en requête agrégée pour éviter une requête SQL par séance.
- Les contraintes de capacité multi-lignes reposent sur un verrou transactionnel PostgreSQL ; une simple `CheckConstraint` ne suffit pas.
- L’envoi SMTP intervient après le commit et ne maintient jamais les verrous de capacité ouverts.
- Le jeton est transmis dans le fragment d’URL, sans outil d’analytics, avec une politique `Referrer-Policy` restrictive.
- Le test de concurrence a été exécuté manuellement sur PostgreSQL. Son intégration à une CI reste à faire ; SQLite l’ignore volontairement.
