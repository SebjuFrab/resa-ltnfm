import csv
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from io import StringIO

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from catalogue.models import Animation, Session, Theme

MAX_ROWS = 500
MAX_GENERATED_SESSIONS = 2_000
DEFAULT_DURATION_MINUTES = 60
DEFAULT_MAX_CAPACITY = 30
DEFAULT_START_TIME = "09:00"
DEFAULT_LOCATION = "À compléter"
REQUIRED_COLUMNS = ()

COLUMN_ALIASES = {
    "titre_animation": {
        "titre_animation",
        "titre",
        "animation",
        "nom_animation",
    },
    "categorie": {
        "categorie",
        "categorie_animation",
        "type",
        "type_animation",
    },
    "thematiques": {
        "thematiques",
        "thematique",
        "themes",
        "theme",
    },
    "lieu_de_rendez_vous": {
        "lieu_de_rendez_vous",
        "lieu_rendez_vous",
        "lieu_de_rdv",
        "lieu_rdv",
        "lieu",
    },
    "duree": {
        "duree",
        "duree_min",
        "duree_minutes",
        "duree_en_minutes",
    },
    "jauge": {
        "jauge",
        "capacite",
        "capacite_max",
        "capacite_maximale",
    },
    "jour": {"jour", "date"},
    "horaires": {
        "horaires",
        "horaire",
        "heures",
        "heure",
        "heures_de_debut",
        "heure_de_debut",
    },
    "responsable": {
        "responsable",
        "organisateur",
        "nom_responsable",
        "responsable_animation",
    },
    "email_responsable": {
        "email_responsable",
        "mail_responsable",
        "courriel_responsable",
        "responsable_email",
    },
}

DAY_NAMES = (
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
)
DAY_ALIASES = {
    "lun": "lundi",
    "mar": "mardi",
    "mer": "mercredi",
    "jeu": "jeudi",
    "ven": "vendredi",
    "sam": "samedi",
    "dim": "dimanche",
}

VENUE_CATEGORY_ALIASES = {
    "salle": Animation.VenueCategory.INDOOR,
    "interieur": Animation.VenueCategory.INDOOR,
    "en_salle": Animation.VenueCategory.INDOOR,
    "indoor": Animation.VenueCategory.INDOOR,
    "exterieur": Animation.VenueCategory.OUTDOOR,
    "dehors": Animation.VenueCategory.OUTDOOR,
    "plein_air": Animation.VenueCategory.OUTDOOR,
    "outdoor": Animation.VenueCategory.OUTDOOR,
}

THEME_ALIASES = {
    "biodiv": "biodiversite",
    "technique_vegetale": "techniques-vegetales",
    "techniques_vegetale": "techniques-vegetales",
    "technique_animale": "techniques-animales",
    "techniques_animale": "techniques-animales",
}


@dataclass(frozen=True)
class ImportIssue:
    line: int | None
    message: str


@dataclass(frozen=True)
class SessionImportRow:
    line: int
    animation_id: int | None
    animation_title: str
    venue_category: str
    venue_category_label: str
    theme_ids: tuple[int, ...]
    theme_names: tuple[str, ...]
    theme_slugs: tuple[str, ...]
    original_venue_category: str | None
    original_theme_ids: tuple[int, ...]
    duration_minutes: int
    date: date
    starts_at: time
    ends_at: time
    location: str
    max_capacity: int
    status: str = Session.Status.OPEN
    organizer: str = ""
    organizer_email: str = ""

    def as_payload(self):
        return {
            "line": self.line,
            "animation_id": self.animation_id,
            "animation_title": self.animation_title,
            "venue_category": self.venue_category,
            "venue_category_label": self.venue_category_label,
            "theme_ids": list(self.theme_ids),
            "theme_names": list(self.theme_names),
            "theme_slugs": list(self.theme_slugs),
            "original_venue_category": self.original_venue_category,
            "original_theme_ids": list(self.original_theme_ids),
            "duration_minutes": self.duration_minutes,
            "date": self.date.isoformat(),
            "starts_at": self.starts_at.isoformat(timespec="minutes"),
            "ends_at": self.ends_at.isoformat(timespec="minutes"),
            "location": self.location,
            "max_capacity": self.max_capacity,
            "status": self.status,
            "organizer": self.organizer,
            "organizer_email": self.organizer_email,
        }


@dataclass
class SessionImportPreview:
    rows: list[SessionImportRow] = field(default_factory=list)
    issues: list[ImportIssue] = field(default_factory=list)

    @property
    def is_valid(self):
        return bool(self.rows) and not self.issues


class SessionImportError(Exception):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("Le fichier d'animations contient des erreurs.")


def _normalize_header(value):
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


def _parse_venue_category(value):
    normalized = _normalize_header(value)
    venue_category = VENUE_CATEGORY_ALIASES.get(normalized)
    if venue_category is None:
        raise ValueError("la catégorie doit être « Salle » ou « Extérieur »")
    return venue_category, Animation.VenueCategory(venue_category).label


def _active_theme_lookup():
    lookup = {}
    ambiguous_tokens = set()
    for theme in Theme.objects.filter(is_active=True):
        for raw_token in (theme.name, theme.slug):
            token = _normalize_header(raw_token)
            existing = lookup.get(token)
            if existing is not None and existing.pk != theme.pk:
                ambiguous_tokens.add(token)
            else:
                lookup[token] = theme
    for token in ambiguous_tokens:
        lookup.pop(token, None)
    return lookup, ambiguous_tokens


def _parse_themes(value, theme_lookup, ambiguous_theme_tokens):
    raw_value = str(value or "").strip()
    if not raw_value:
        return ()

    raw_tokens = [part.strip() for part in raw_value.split("|")]
    if any(not token for token in raw_tokens):
        raise ValueError("la liste des thématiques contient une valeur vide")

    themes = []
    seen_ids = set()
    for raw_token in raw_tokens:
        token = _normalize_header(raw_token)
        alias = THEME_ALIASES.get(token)
        if alias is not None:
            token = _normalize_header(alias)
        if token in ambiguous_theme_tokens:
            raise ValueError(f"la thématique « {raw_token} » est ambiguë")
        theme = theme_lookup.get(token)
        if theme is None:
            raise ValueError(f"la thématique « {raw_token} » est inconnue ou inactive")
        if theme.pk in seen_ids:
            raise ValueError(f"la thématique « {raw_token} » est dupliquée")
        seen_ids.add(theme.pk)
        themes.append(theme)

    return tuple(sorted(themes, key=lambda theme: (theme.sort_order, theme.name, theme.pk)))


def _parse_date(value):
    for date_format in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value).strip(), date_format).date()
        except ValueError:
            continue
    raise ValueError("jour attendu : mercredi, jeudi ou une date du salon")


def _event_dates():
    result = []
    for configured_date in settings.EVENT_DATES:
        try:
            result.append(date.fromisoformat(str(configured_date)))
        except ValueError as error:
            raise ValueError("la configuration des jours du salon n'est pas valide") from error
    return result


def _parse_day(value):
    raw_value = str(value or "").strip()
    configured_dates = _event_dates()
    if not raw_value:
        if not configured_dates:
            raise ValueError("aucun jour du salon n'est configuré")
        return configured_dates[0]

    try:
        parsed_date = _parse_date(raw_value)
    except ValueError:
        normalized_day = _normalize_header(raw_value)
        normalized_day = DAY_ALIASES.get(normalized_day, normalized_day)
        matching_dates = [
            event_date
            for event_date in configured_dates
            if DAY_NAMES[event_date.weekday()] == normalized_day
        ]
        if not matching_dates:
            expected_days = ", ".join(
                dict.fromkeys(DAY_NAMES[event_date.weekday()] for event_date in configured_dates)
            )
            suffix = f" ({expected_days})" if expected_days else ""
            raise ValueError(f"le jour ne correspond pas à un jour du salon{suffix}") from None
        if len(matching_dates) > 1:
            raise ValueError(
                "le nom du jour est ambigu dans cette édition ; utilisez une date complète"
            ) from None
        return matching_dates[0]

    if configured_dates and parsed_date not in configured_dates:
        raise ValueError("la date ne correspond pas à un jour du salon")
    return parsed_date


def _parse_time(value):
    raw_value = str(value or "").strip().lower().replace("h", ":")
    if raw_value.endswith(":"):
        raw_value += "00"
    for time_format in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw_value, time_format).time()
        except ValueError:
            continue
    raise ValueError("heure attendue au format HH:MM")


def _parse_positive_integer(value, label):
    raw_value = str(value or "").strip()
    try:
        parsed_value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{label} doit être un nombre entier") from error
    if parsed_value <= 0:
        raise ValueError(f"{label} doit être strictement positive")
    return parsed_value


def _parse_duration(value):
    raw_value = str(value or "").strip().lower().replace(" ", "")
    if raw_value.isdigit():
        return _parse_positive_integer(raw_value, "la durée")

    minutes_match = re.fullmatch(r"(\d+)min(?:ute)?s?", raw_value)
    if minutes_match:
        return _parse_positive_integer(minutes_match.group(1), "la durée")

    hours_match = re.fullmatch(r"(\d+)h(?:(\d{1,2}))?", raw_value)
    if hours_match:
        hours = int(hours_match.group(1))
        minutes = int(hours_match.group(2) or 0)
        if minutes >= 60 or hours == minutes == 0:
            raise ValueError("la durée n'est pas valide")
        return hours * 60 + minutes

    clock_match = re.fullmatch(r"(\d+):(\d{2})", raw_value)
    if clock_match:
        hours = int(clock_match.group(1))
        minutes = int(clock_match.group(2))
        if minutes < 60 and (hours or minutes):
            return hours * 60 + minutes

    raise ValueError("durée attendue en minutes ou au format 1h/1h30")


def _validate_text(value, *, label, max_length, required=True):
    cleaned_value = str(value or "").strip()
    if required and not cleaned_value:
        raise ValueError(f"{label} est obligatoire")
    if len(cleaned_value) > max_length:
        raise ValueError(f"{label} ne doit pas dépasser {max_length} caractères")
    return cleaned_value


def _validate_optional_contact(mapping):
    organizer = str(mapping.get("responsable", "") or "").strip()
    organizer_email = str(mapping.get("email_responsable", "") or "").strip()
    if len(organizer) > 200:
        raise ValueError("le responsable ne doit pas dépasser 200 caractères")
    if organizer_email:
        try:
            validate_email(organizer_email)
        except ValidationError as error:
            raise ValueError("le courriel du responsable n'est pas valide") from error
    return organizer, organizer_email


def _find_animation(title):
    animations = list(
        Animation.objects.filter(title__iexact=title).prefetch_related("themes").distinct()[:2]
    )
    if len(animations) > 1:
        raise ValueError(f"animation ambiguë : {title}")
    return animations[0] if animations else None


def _rows_from_mapping(mapping, line, theme_lookup, ambiguous_theme_tokens):
    title = _validate_text(
        mapping["titre_animation"],
        label="le titre de l'animation",
        max_length=200,
        required=False,
    )
    title = title or f"Animation à compléter — ligne {line}"
    animation = _find_animation(title)

    if str(mapping["categorie"] or "").strip():
        venue_category, venue_category_label = _parse_venue_category(mapping["categorie"])
    else:
        venue_category = (
            animation.venue_category
            if animation and animation.venue_category
            else Animation.VenueCategory.INDOOR
        )
        venue_category_label = Animation.VenueCategory(venue_category).label

    if str(mapping["thematiques"] or "").strip():
        themes = _parse_themes(
            mapping["thematiques"],
            theme_lookup,
            ambiguous_theme_tokens,
        )
    elif animation:
        themes = tuple(
            sorted(
                animation.themes.all(),
                key=lambda theme: (theme.sort_order, theme.name, theme.pk),
            )
        )
    else:
        themes = ()

    location = _validate_text(
        mapping["lieu_de_rendez_vous"],
        label="le lieu de rendez-vous",
        max_length=200,
        required=False,
    )
    location = location or DEFAULT_LOCATION
    duration_minutes = (
        _parse_duration(mapping["duree"])
        if str(mapping["duree"] or "").strip()
        else (animation.indicative_duration if animation else DEFAULT_DURATION_MINUTES)
    )
    max_capacity = (
        _parse_positive_integer(mapping["jauge"], "la jauge")
        if str(mapping["jauge"] or "").strip()
        else DEFAULT_MAX_CAPACITY
    )
    session_date = _parse_day(mapping["jour"])
    organizer, organizer_email = _validate_optional_contact(mapping)

    if animation and animation.indicative_duration != duration_minutes:
        raise ValueError(
            f"la durée ne correspond pas aux {animation.indicative_duration} minutes "
            "de l'animation existante"
        )
    original_theme_ids = (
        tuple(sorted(theme.pk for theme in animation.themes.all())) if animation else ()
    )

    raw_times = str(mapping["horaires"] or "").strip()
    if not raw_times:
        raw_times = DEFAULT_START_TIME
    time_values = [part.strip() for part in raw_times.split(",")]
    if any(not value for value in time_values):
        raise ValueError("la liste des horaires contient une valeur vide")

    starts = [_parse_time(value) for value in time_values]
    if len(set(starts)) != len(starts):
        raise ValueError("un horaire est dupliqué sur cette ligne")

    rows = []
    for starts_at in starts:
        starts_datetime = datetime.combine(session_date, starts_at)
        ends_datetime = starts_datetime + timedelta(minutes=duration_minutes)
        if ends_datetime.date() != session_date:
            raise ValueError("une séance ne peut pas se terminer le lendemain")
        rows.append(
            SessionImportRow(
                line=line,
                animation_id=animation.pk if animation else None,
                animation_title=animation.title if animation else title,
                venue_category=venue_category,
                venue_category_label=venue_category_label,
                theme_ids=tuple(theme.pk for theme in themes),
                theme_names=tuple(theme.name for theme in themes),
                theme_slugs=tuple(theme.slug for theme in themes),
                original_venue_category=(animation.venue_category if animation else None),
                original_theme_ids=original_theme_ids,
                duration_minutes=duration_minutes,
                date=session_date,
                starts_at=starts_at,
                ends_at=ends_datetime.time(),
                location=location,
                max_capacity=max_capacity,
                organizer=organizer,
                organizer_email=organizer_email,
            )
        )

    return rows


def _decode_csv(upload):
    raw_content = upload.read()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw_content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Le fichier doit être encodé en UTF-8 ou Windows-1252.")


def _csv_reader(content):
    content = content.lstrip("\r\n")
    if not content.strip():
        raise ValueError("Le fichier est vide.")
    if "\x00" in content:
        raise ValueError("Le fichier CSV n'est pas valide.")

    header_line = content.splitlines()[0]
    semicolon_count = header_line.count(";")
    comma_count = header_line.count(",")
    delimiter = ";" if semicolon_count >= comma_count and semicolon_count else ","
    return csv.DictReader(StringIO(content), delimiter=delimiter)


def _header_mapping(fieldnames):
    canonical_by_header = {}
    used_columns = {}
    for raw_header in fieldnames or ():
        normalized_header = _normalize_header(raw_header)
        canonical = next(
            (column for column, aliases in COLUMN_ALIASES.items() if normalized_header in aliases),
            None,
        )
        if canonical is None:
            continue
        if canonical in used_columns:
            raise ValueError(f"Colonnes en double : {used_columns[canonical]} et {raw_header}.")
        used_columns[canonical] = str(raw_header)
        canonical_by_header[raw_header] = canonical

    if not canonical_by_header:
        raise ValueError("Aucune colonne reconnue. Utilisez au moins un intitulé du modèle CSV.")
    return canonical_by_header


def _natural_key(import_row):
    animation_key = import_row.animation_id or import_row.animation_title.strip().casefold()
    return (
        animation_key,
        import_row.date,
        import_row.starts_at,
        import_row.location.casefold(),
    )


def _existing_session(import_row):
    if import_row.animation_id is None:
        return False
    return Session.objects.filter(
        animation_id=import_row.animation_id,
        date=import_row.date,
        starts_at=import_row.starts_at,
        location__iexact=import_row.location,
    ).exists()


def preview_session_csv(upload):
    """Prévisualise un CSV d'animations sans effectuer aucune écriture."""
    preview = SessionImportPreview()
    try:
        content = _decode_csv(upload)
        reader = _csv_reader(content)
        header_mapping = _header_mapping(reader.fieldnames)
        theme_lookup, ambiguous_theme_tokens = _active_theme_lookup()
    except (csv.Error, ValueError) as error:
        preview.issues.append(ImportIssue(None, str(error)))
        return preview

    seen_sessions = set()
    animation_signatures = {}
    source_row_count = 0
    for index, raw_row in enumerate(reader, start=2):
        if None in raw_row:
            preview.issues.append(
                ImportIssue(
                    index,
                    "La ligne contient des cellules supplémentaires. Dans un CSV séparé par "
                    "des virgules, entourez la liste des horaires de guillemets.",
                )
            )
            continue

        row = {column: "" for column in COLUMN_ALIASES}
        row.update(
            {
                canonical: str(raw_row.get(raw_header, "") or "")
                for raw_header, canonical in header_mapping.items()
            }
        )
        if not any(value.strip() for value in row.values()):
            continue
        source_row_count += 1
        if source_row_count > MAX_ROWS:
            preview.issues.append(
                ImportIssue(None, f"Le fichier ne doit pas dépasser {MAX_ROWS} lignes.")
            )
            break

        try:
            import_rows = _rows_from_mapping(
                row,
                index,
                theme_lookup,
                ambiguous_theme_tokens,
            )
            title_key = import_rows[0].animation_title.strip().casefold()
            animation_signature = (
                import_rows[0].duration_minutes,
                import_rows[0].venue_category,
                frozenset(import_rows[0].theme_ids),
            )
            previous_signature = animation_signatures.setdefault(
                title_key,
                animation_signature,
            )
            if previous_signature != animation_signature:
                raise ValueError(
                    "une même animation possède des informations différentes dans le fichier"
                )
        except (KeyError, ValueError) as error:
            preview.issues.append(ImportIssue(index, str(error)))
            continue

        for import_row in import_rows:
            natural_key = _natural_key(import_row)
            if natural_key in seen_sessions:
                preview.issues.append(
                    ImportIssue(index, "Cette séance est dupliquée dans le fichier.")
                )
                continue
            seen_sessions.add(natural_key)
            if _existing_session(import_row):
                preview.issues.append(ImportIssue(index, "Cette séance existe déjà."))
                continue
            preview.rows.append(import_row)
            if len(preview.rows) > MAX_GENERATED_SESSIONS:
                preview.rows.pop()
                preview.issues.append(
                    ImportIssue(
                        None,
                        f"Le fichier génère plus de {MAX_GENERATED_SESSIONS} séances.",
                    )
                )
                break
        if len(preview.rows) >= MAX_GENERATED_SESSIONS and preview.issues:
            break

    if not preview.rows and not preview.issues:
        preview.issues.append(ImportIssue(None, "Le fichier ne contient aucune séance."))
    return preview


def _payload_row(payload):
    try:
        line = int(payload["line"])
        animation_id_value = payload.get("animation_id")
        animation_id = int(animation_id_value) if animation_id_value is not None else None
        animation_title = _validate_text(
            payload["animation_title"],
            label="le titre de l'animation",
            max_length=200,
        )
        venue_category = _validate_text(
            payload["venue_category"],
            label="la catégorie",
            max_length=7,
        )
        if venue_category not in Animation.VenueCategory.values:
            raise ValueError("la catégorie n'est plus valide")
        venue_category_label = _validate_text(
            payload["venue_category_label"],
            label="le libellé de la catégorie",
            max_length=100,
        )
        if venue_category_label != Animation.VenueCategory(venue_category).label:
            raise ValueError("le libellé de la catégorie n'est plus valide")

        raw_theme_ids = payload["theme_ids"]
        raw_theme_names = payload["theme_names"]
        raw_theme_slugs = payload["theme_slugs"]
        if not all(
            isinstance(values, (list, tuple))
            for values in (raw_theme_ids, raw_theme_names, raw_theme_slugs)
        ):
            raise ValueError("les thématiques de l'aperçu ne sont plus valides")
        if not (len(raw_theme_ids) == len(raw_theme_names) == len(raw_theme_slugs)):
            raise ValueError("les thématiques de l'aperçu ne sont plus valides")
        theme_ids = tuple(
            _parse_positive_integer(value, "l'identifiant de thématique") for value in raw_theme_ids
        )
        theme_names = tuple(
            _validate_text(value, label="la thématique", max_length=100)
            for value in raw_theme_names
        )
        theme_slugs = tuple(
            _validate_text(value, label="la thématique", max_length=50) for value in raw_theme_slugs
        )
        if len(set(theme_ids)) != len(theme_ids) or len(set(theme_slugs)) != len(theme_slugs):
            raise ValueError("une thématique est dupliquée dans l'aperçu")

        original_venue_category = payload["original_venue_category"]
        if original_venue_category is not None:
            original_venue_category = _validate_text(
                original_venue_category,
                label="la catégorie d'origine",
                max_length=7,
            )
            if original_venue_category not in Animation.VenueCategory.values:
                raise ValueError("la catégorie d'origine n'est plus valide")
        raw_original_theme_ids = payload["original_theme_ids"]
        if not isinstance(raw_original_theme_ids, (list, tuple)):
            raise ValueError("les thématiques d'origine ne sont plus valides")
        original_theme_ids = tuple(
            _parse_positive_integer(value, "l'identifiant de thématique d'origine")
            for value in raw_original_theme_ids
        )
        if len(set(original_theme_ids)) != len(original_theme_ids):
            raise ValueError("une thématique d'origine est dupliquée dans l'aperçu")
        if animation_id is None and (original_venue_category is not None or original_theme_ids):
            raise ValueError("les données d'origine de l'animation sont incohérentes")

        duration_minutes = _parse_positive_integer(payload["duration_minutes"], "la durée")
        session_date = date.fromisoformat(str(payload["date"]))
        starts_at = time.fromisoformat(str(payload["starts_at"]))
        ends_at = time.fromisoformat(str(payload["ends_at"]))
        location = _validate_text(
            payload["location"], label="le lieu de rendez-vous", max_length=200
        )
        max_capacity = _parse_positive_integer(payload["max_capacity"], "la jauge")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("les données de l'aperçu ne sont plus valides") from error

    configured_dates = _event_dates()
    if configured_dates and session_date not in configured_dates:
        raise ValueError("la date ne correspond pas à un jour du salon")
    if ends_at <= starts_at:
        raise ValueError("l'heure de fin doit être postérieure à l'heure de début")
    expected_end = datetime.combine(session_date, starts_at) + timedelta(minutes=duration_minutes)
    if expected_end.date() != session_date or expected_end.time() != ends_at:
        raise ValueError("la durée et l'heure de fin de l'aperçu sont incohérentes")
    if payload.get("status") != Session.Status.OPEN:
        raise ValueError("le statut de l'aperçu n'est plus valide")

    organizer, organizer_email = _validate_optional_contact(
        {
            "responsable": payload.get("organizer", ""),
            "email_responsable": payload.get("organizer_email", ""),
        }
    )
    return SessionImportRow(
        line=line,
        animation_id=animation_id,
        animation_title=animation_title,
        venue_category=venue_category,
        venue_category_label=venue_category_label,
        theme_ids=theme_ids,
        theme_names=theme_names,
        theme_slugs=theme_slugs,
        original_venue_category=original_venue_category,
        original_theme_ids=original_theme_ids,
        duration_minutes=duration_minutes,
        date=session_date,
        starts_at=starts_at,
        ends_at=ends_at,
        location=location,
        max_capacity=max_capacity,
        organizer=organizer,
        organizer_email=organizer_email,
    )


def _available_animation_slug(title):
    max_length = Animation._meta.get_field("slug").max_length
    base = (slugify(title) or "animation")[:max_length]
    candidate = base
    suffix_number = 2
    while Animation.objects.filter(slug=candidate).exists():
        suffix = f"-{suffix_number}"
        candidate = f"{base[: max_length - len(suffix)]}{suffix}"
        suffix_number += 1
    return candidate


def _animation_signature(import_row):
    return (
        import_row.animation_id,
        import_row.duration_minutes,
        import_row.venue_category,
        import_row.venue_category_label,
        import_row.original_venue_category,
        frozenset(import_row.original_theme_ids),
        frozenset(
            zip(
                import_row.theme_ids,
                import_row.theme_names,
                import_row.theme_slugs,
                strict=True,
            )
        ),
    )


def _locked_import_themes(import_rows, issues):
    expected_ids = {theme_id for import_row in import_rows for theme_id in import_row.theme_ids}
    themes_by_id = {
        theme.pk: theme for theme in Theme.objects.select_for_update().filter(pk__in=expected_ids)
    }
    for import_row in import_rows:
        for theme_id, theme_name, theme_slug in zip(
            import_row.theme_ids,
            import_row.theme_names,
            import_row.theme_slugs,
            strict=True,
        ):
            theme = themes_by_id.get(theme_id)
            if (
                theme is None
                or not theme.is_active
                or theme.name != theme_name
                or theme.slug != theme_slug
            ):
                issues.append(
                    ImportIssue(
                        import_row.line,
                        "une thématique a été modifiée depuis la prévisualisation",
                    )
                )
                break
    return themes_by_id


def _animation_unchanged_since_preview(animation, reference):
    return (
        animation.title.strip().casefold() == reference.animation_title.strip().casefold()
        and animation.indicative_duration == reference.duration_minutes
        and animation.venue_category == reference.original_venue_category
        and {theme.pk for theme in animation.themes.all()} == set(reference.original_theme_ids)
    )


def _animation_matches_import(animation, reference):
    return (
        animation.title.strip().casefold() == reference.animation_title.strip().casefold()
        and animation.indicative_duration == reference.duration_minutes
        and animation.venue_category == reference.venue_category
        and {theme.pk for theme in animation.themes.all()} == set(reference.theme_ids)
    )


def _resolve_animations(import_rows, issues):
    themes_by_id = _locked_import_themes(import_rows, issues)
    rows_by_title = {}
    for import_row in import_rows:
        rows_by_title.setdefault(import_row.animation_title.strip().casefold(), []).append(
            import_row
        )

    resolved = {}
    missing = []
    for title_key, title_rows in rows_by_title.items():
        reference = title_rows[0]
        signatures = {_animation_signature(row) for row in title_rows}
        if len(signatures) != 1:
            issues.append(
                ImportIssue(reference.line, "les données de l'animation sont incohérentes")
            )
            continue

        expected_id = reference.animation_id
        if expected_id is not None:
            animation = (
                Animation.objects.select_for_update()
                .prefetch_related("themes")
                .filter(pk=expected_id)
                .first()
            )
            if animation is None or not _animation_unchanged_since_preview(
                animation,
                reference,
            ):
                issues.append(
                    ImportIssue(
                        reference.line,
                        "l'animation a été modifiée depuis la prévisualisation",
                    )
                )
                continue
            animation.venue_category = reference.venue_category
            try:
                animation.full_clean()
                with transaction.atomic():
                    animation.save(update_fields=("venue_category", "updated_at"))
                    animation.themes.set(
                        [themes_by_id[theme_id] for theme_id in reference.theme_ids]
                    )
            except (IntegrityError, ValidationError) as error:
                issues.append(ImportIssue(reference.line, str(error)))
                continue
            resolved[title_key] = animation
            continue

        matches = list(
            Animation.objects.select_for_update()
            .prefetch_related("themes")
            .filter(title__iexact=reference.animation_title)[:2]
        )
        if len(matches) > 1:
            issues.append(
                ImportIssue(reference.line, f"animation ambiguë : {reference.animation_title}")
            )
        elif matches:
            animation = matches[0]
            if not _animation_matches_import(animation, reference):
                issues.append(
                    ImportIssue(
                        reference.line,
                        "une animation du même titre existe maintenant avec d'autres informations",
                    )
                )
            else:
                resolved[title_key] = animation
        else:
            missing.append((title_key, reference))

    if issues:
        return resolved

    for title_key, reference in missing:
        animation = Animation(
            title=reference.animation_title,
            slug=_available_animation_slug(reference.animation_title),
            short_description=reference.animation_title,
            venue_category=reference.venue_category,
            indicative_duration=reference.duration_minutes,
        )
        try:
            animation.full_clean()
            with transaction.atomic():
                animation.save()
                animation.themes.set([themes_by_id[theme_id] for theme_id in reference.theme_ids])
        except (IntegrityError, ValidationError) as error:
            issues.append(ImportIssue(reference.line, str(error)))
        else:
            resolved[title_key] = animation
    return resolved


def _session_from_import_row(import_row, animation):
    session = Session(
        animation=animation,
        date=import_row.date,
        starts_at=import_row.starts_at,
        ends_at=import_row.ends_at,
        location=import_row.location,
        max_capacity=import_row.max_capacity,
        status=Session.Status.OPEN,
        organizer=import_row.organizer,
    )
    if hasattr(session, "organizer_email"):
        session.organizer_email = import_row.organizer_email
    return session


def import_session_payload(payload_rows):
    """Revalide un aperçu et crée animations et séances en une transaction."""
    if (
        not isinstance(payload_rows, list)
        or not payload_rows
        or len(payload_rows) > MAX_GENERATED_SESSIONS
    ):
        raise SessionImportError([ImportIssue(None, "L'aperçu n'est pas valide.")])

    issues = []
    import_rows = []
    seen = set()
    for payload in payload_rows:
        try:
            if not isinstance(payload, dict):
                raise TypeError("une ligne de l'aperçu n'est pas valide")
            import_row = _payload_row(payload)
            natural_key = _natural_key(import_row)
            if natural_key in seen:
                raise ValueError("cette séance est dupliquée dans l'aperçu")
            seen.add(natural_key)
        except (TypeError, ValueError, ValidationError) as error:
            issue_line = payload.get("line") if isinstance(payload, dict) else None
            issues.append(ImportIssue(issue_line, str(error)))
        else:
            import_rows.append(import_row)

    if issues:
        raise SessionImportError(issues)

    sessions = []
    with transaction.atomic():
        animations = _resolve_animations(import_rows, issues)
        dates = {import_row.date for import_row in import_rows}
        list(Session.objects.select_for_update().filter(date__in=dates).values_list("pk"))

        for import_row in import_rows:
            title_key = import_row.animation_title.strip().casefold()
            animation = animations.get(title_key)
            if animation is None:
                continue
            session = _session_from_import_row(import_row, animation)
            if Session.objects.filter(
                animation=animation,
                date=session.date,
                starts_at=session.starts_at,
                location__iexact=session.location,
            ).exists():
                issues.append(ImportIssue(import_row.line, "cette séance existe déjà"))
                continue
            try:
                session.full_clean()
            except ValidationError as error:
                issues.append(ImportIssue(import_row.line, str(error)))
            else:
                sessions.append(session)

        if not sessions and not issues:
            issues.append(ImportIssue(None, "L'aperçu ne contient aucune séance."))
        if issues:
            raise SessionImportError(issues)
        try:
            Session.objects.bulk_create(sessions)
        except IntegrityError as error:
            raise SessionImportError(
                [
                    ImportIssue(
                        None,
                        "Une séance identique vient d’être créée. Rechargez l’aperçu.",
                    )
                ]
            ) from error

    return sessions
