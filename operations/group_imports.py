"""Import CSV transactionnel des groupes par l'équipe du salon."""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from io import StringIO

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

from catalogue.models import SchoolLevel
from inscriptions.choices import DEPARTMENT_CHOICES
from inscriptions.codes import normalize_group_code
from inscriptions.models import (
    GroupFamily,
    Institution,
    Registration,
    RegistrationEvent,
    Teacher,
)
from inscriptions.services.registration import RegistrationError, create_draft

MAX_ROWS = 500
MAX_STUDENTS = 500
MAX_CHAPERONES = 100
PAYLOAD_SALT = "operations.group-import-row.v1"

GROUP_IMPORT_COLUMNS = (
    "nom_enseignant",
    "prenom_enseignant",
    "email_enseignant",
    "telephone_enseignant",
    "etablissement",
    "type_etablissement",
    "commune",
    "departement",
    "code_groupe",
    "famille",
    "niveau",
    "jour",
    "nb_etudiants",
    "nb_accompagnateurs",
    "effectif_total",
    "remarque_niveau",
    "remarque_generale",
)

OPTIONAL_COLUMNS = {
    "remarque_niveau",
    "remarque_generale",
}
REQUIRED_COLUMNS = tuple(
    column for column in GROUP_IMPORT_COLUMNS if column not in OPTIONAL_COLUMNS
)

COLUMN_ALIASES = {
    "nom_enseignant": {
        "nom_enseignant",
        "nom_du_professeur",
        "nom_professeur",
        "nom_prof",
        "enseignant_nom",
    },
    "prenom_enseignant": {
        "prenom_enseignant",
        "prenom_du_professeur",
        "prenom_professeur",
        "prenom_prof",
        "enseignant_prenom",
    },
    "email_enseignant": {
        "email_enseignant",
        "mail_enseignant",
        "courriel_enseignant",
        "email_professeur",
        "mail_professeur",
        "email_prof",
    },
    "telephone_enseignant": {
        "telephone_enseignant",
        "tel_enseignant",
        "telephone_professeur",
        "tel_professeur",
        "telephone_prof",
        "tel_prof",
    },
    "etablissement": {
        "etablissement",
        "nom_etablissement",
        "etablissement_nom",
        "structure",
    },
    "type_etablissement": {
        "type_etablissement",
        "categorie_etablissement",
        "statut_etablissement",
        "type_structure",
    },
    "commune": {"commune", "ville", "localite"},
    "departement": {"departement", "dept", "numero_departement"},
    "code_groupe": {
        "code_groupe",
        "nom_code",
        "code",
        "identifiant_groupe",
    },
    "famille": {"famille", "famille_groupe", "categorie", "categorie_groupe"},
    "niveau": {"niveau", "niveau_scolaire", "classe"},
    "jour": {"jour", "date", "date_visite", "jour_visite"},
    "nb_etudiants": {
        "nb_etudiants",
        "nombre_etudiants",
        "nb_eleves",
        "nombre_eleves",
        "effectif_etudiants",
        "effectif_eleves",
    },
    "nb_accompagnateurs": {
        "nb_accompagnateurs",
        "nombre_accompagnateurs",
        "nb_accompagnants",
        "nombre_accompagnants",
        "nb_professeurs",
        "nombre_professeurs",
        "nb_profs",
    },
    "effectif_total": {
        "effectif_total",
        "effectif",
        "effectif_groupe",
        "total",
    },
    "remarque_niveau": {
        "remarque_niveau",
        "commentaire_niveau",
        "precision_niveau",
    },
    "remarque_generale": {
        "remarque_generale",
        "remarque",
        "commentaire",
        "commentaire_general",
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

ALLOWED_DEPARTMENTS = {
    value
    for _group_label, choices in DEPARTMENT_CHOICES
    for value, _label in choices
}

INSTITUTION_TYPE_ALIASES = {
    "ecole_primaire": Institution.Type.PRIMARY_SCHOOL,
    "primaire": Institution.Type.PRIMARY_SCHOOL,
    "college": Institution.Type.MIDDLE_SCHOOL,
    "lycee": Institution.Type.HIGH_SCHOOL,
    "lycee_general": Institution.Type.HIGH_SCHOOL,
    "lycee_general_et_technologique": Institution.Type.HIGH_SCHOOL,
    "lycee_professionnel": Institution.Type.HIGH_SCHOOL,
    "agricole": Institution.Type.AGRICULTURAL,
    "etablissement_agricole": Institution.Type.AGRICULTURAL,
    "lycee_agricole": Institution.Type.AGRICULTURAL,
    "enseignement_superieur": Institution.Type.HIGHER_EDUCATION,
    "superieur": Institution.Type.HIGHER_EDUCATION,
    "universite": Institution.Type.HIGHER_EDUCATION,
    "autre": Institution.Type.OTHER,
}


@dataclass(frozen=True, slots=True)
class GroupImportIssue:
    line: int | None
    message: str


@dataclass(frozen=True, slots=True)
class GroupImportRow:
    line: int
    teacher_last_name: str
    teacher_first_name: str
    teacher_email: str
    teacher_phone: str
    institution_name: str
    institution_type: str
    institution_city: str
    institution_department: str
    group_code: str
    family_id: int
    family_slug: str
    family_name: str
    school_level_id: int
    school_level_code: str
    school_level_label: str
    visit_date: date
    student_count: int
    chaperone_count: int
    total_count: int
    level_comment: str = ""
    comment: str = ""
    institution_id: int | None = None
    teacher_id: int | None = None

    @property
    def institution_type_label(self):
        return Institution.Type(self.institution_type).label

    @property
    def effectif_total(self):
        """Alias métier utilisable directement dans l'aperçu."""
        return self.total_count

    def _payload_data(self):
        return {
            "line": self.line,
            "teacher_last_name": self.teacher_last_name,
            "teacher_first_name": self.teacher_first_name,
            "teacher_email": self.teacher_email,
            "teacher_phone": self.teacher_phone,
            "institution_name": self.institution_name,
            "institution_type": self.institution_type,
            "institution_city": self.institution_city,
            "institution_department": self.institution_department,
            "institution_id": self.institution_id,
            "teacher_id": self.teacher_id,
            "group_code": self.group_code,
            "family_id": self.family_id,
            "family_slug": self.family_slug,
            "family_name": self.family_name,
            "school_level_id": self.school_level_id,
            "school_level_code": self.school_level_code,
            "school_level_label": self.school_level_label,
            "visit_date": self.visit_date.isoformat(),
            "student_count": self.student_count,
            "chaperone_count": self.chaperone_count,
            "total_count": self.total_count,
            "effectif_total": self.total_count,
            "level_comment": self.level_comment,
            "comment": self.comment,
        }

    def as_payload(self):
        data = self._payload_data()
        return {
            **data,
            "_seal": signing.dumps(data, salt=PAYLOAD_SALT, compress=True),
        }


@dataclass(slots=True)
class GroupImportPreview:
    rows: list[GroupImportRow] = field(default_factory=list)
    issues: list[GroupImportIssue] = field(default_factory=list)

    @property
    def is_valid(self):
        return bool(self.rows) and not self.issues


class GroupImportError(Exception):
    def __init__(self, issues):
        self.issues = list(issues)
        super().__init__("Le fichier de groupes contient des erreurs.")


def _normalize(value):
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


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
    if not semicolon_count and not comma_count:
        raise ValueError("Le séparateur du fichier doit être un point-virgule ou une virgule.")
    delimiter = ";" if semicolon_count >= comma_count else ","
    return csv.DictReader(StringIO(content), delimiter=delimiter)


def _header_mapping(fieldnames):
    canonical_by_header = {}
    used_columns = {}
    for raw_header in fieldnames or ():
        normalized_header = _normalize(raw_header)
        canonical = next(
            (
                column
                for column, aliases in COLUMN_ALIASES.items()
                if normalized_header in aliases
            ),
            None,
        )
        if canonical is None:
            continue
        if canonical in used_columns:
            raise ValueError(
                f"Colonnes en double : {used_columns[canonical]} et {raw_header}."
            )
        used_columns[canonical] = str(raw_header)
        canonical_by_header[raw_header] = canonical

    missing = [column for column in REQUIRED_COLUMNS if column not in used_columns]
    if missing:
        raise ValueError(f"Colonnes manquantes : {', '.join(missing)}.")
    return canonical_by_header


def _text(value, *, label, max_length, required=True):
    cleaned = str(value or "").strip()
    if required and not cleaned:
        raise ValueError(f"{label} est obligatoire")
    if len(cleaned) > max_length:
        raise ValueError(f"{label} ne doit pas dépasser {max_length} caractères")
    return cleaned


def _email(value):
    cleaned = _text(value, label="le courriel de l'enseignant", max_length=254)
    try:
        validate_email(cleaned)
    except ValidationError as error:
        raise ValueError("le courriel de l'enseignant n'est pas valide") from error
    return cleaned.lower()


def _integer(value, *, label, minimum, maximum):
    raw_value = str(value or "").strip()
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} doit être un nombre entier") from error
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} doit être compris entre {minimum} et {maximum}")
    return parsed


def _event_dates():
    try:
        return tuple(date.fromisoformat(str(value)) for value in settings.EVENT_DATES)
    except (TypeError, ValueError) as error:
        raise ValueError("la configuration des jours du salon n'est pas valide") from error


def _day(value):
    raw_value = str(value or "").strip()
    if not raw_value:
        raise ValueError("le jour est obligatoire")
    configured_dates = _event_dates()
    parsed = None
    for date_format in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(raw_value, date_format).date()
            break
        except ValueError:
            continue
    if parsed is not None:
        if parsed not in configured_dates:
            raise ValueError("la date ne correspond pas à un jour du salon")
        return parsed

    normalized_day = DAY_ALIASES.get(_normalize(raw_value), _normalize(raw_value))
    matching_dates = [
        event_date
        for event_date in configured_dates
        if DAY_NAMES[event_date.weekday()] == normalized_day
    ]
    if not matching_dates:
        raise ValueError("le jour ne correspond pas à un jour du salon")
    if len(matching_dates) > 1:
        raise ValueError("le nom du jour est ambigu ; utilisez une date complète")
    return matching_dates[0]


def _department(value):
    raw_value = str(value or "").strip()
    match = re.fullmatch(r"(\d{2,3})(?:\s*(?:-|\u2013|\u2014|:)\s*.+)?", raw_value)
    department = match.group(1) if match else raw_value
    if department not in ALLOWED_DEPARTMENTS:
        allowed = ", ".join(sorted(ALLOWED_DEPARTMENTS))
        raise ValueError(f"le département doit être l'un des suivants : {allowed}")
    return department


def _institution_type(value):
    normalized = _normalize(value)
    choices = {
        _normalize(choice.value): choice.value for choice in Institution.Type
    }
    choices.update({_normalize(choice.label): choice.value for choice in Institution.Type})
    choices.update(INSTITUTION_TYPE_ALIASES)
    try:
        return choices[normalized]
    except KeyError as error:
        labels = ", ".join(choice.label for choice in Institution.Type)
        raise ValueError(f"le type d'établissement est inconnu ({labels})") from error


def _matching_objects(queryset, value, *, fields):
    lookup = _normalize(value)
    return [
        instance
        for instance in queryset
        if any(_normalize(getattr(instance, field_name)) == lookup for field_name in fields)
    ]


def _resolve_family(value):
    value = _text(value, label="la famille", max_length=120)
    all_families = list(GroupFamily.objects.all())
    matches = _matching_objects(all_families, value, fields=("slug", "name"))
    matches = list({family.pk: family for family in matches}.values())
    active_matches = [family for family in matches if family.is_active]
    if len(active_matches) > 1:
        raise ValueError(f"la famille est ambiguë : {value}")
    if active_matches:
        return active_matches[0]
    if matches:
        raise ValueError(f"la famille est inactive : {value}")
    raise ValueError(f"la famille est inconnue : {value}")


def _resolve_school_level(value):
    value = _text(value, label="le niveau", max_length=100)
    all_levels = list(SchoolLevel.objects.all())
    matches = _matching_objects(all_levels, value, fields=("code", "label"))
    matches = list({level.pk: level for level in matches}.values())
    active_matches = [level for level in matches if level.is_active]
    if len(active_matches) > 1:
        raise ValueError(f"le niveau est ambigu : {value}")
    if active_matches:
        return active_matches[0]
    if matches:
        raise ValueError(f"le niveau est inactif : {value}")
    raise ValueError(f"le niveau est inconnu : {value}")


def _same_text(first, second):
    return str(first or "").strip().casefold() == str(second or "").strip().casefold()


def _find_institution(*, name, city, department):
    matches = list(
        Institution.objects.filter(
            name__iexact=name,
            city__iexact=city,
            department=department,
        )[:2]
    )
    if len(matches) > 1:
        raise ValueError(
            "plusieurs établissements correspondent au même nom, à la même commune "
            "et au même département"
        )
    return matches[0] if matches else None


def _assert_institution_compatible(institution, institution_type):
    if institution.institution_type != institution_type:
        raise ValueError(
            "le type de l'établissement existant diffère du CSV ; "
            "aucune donnée existante n'a été modifiée"
        )


def _find_teacher(*, institution, email, first_name, last_name, phone):
    candidates = list(
        Teacher.objects.filter(institution=institution, email__iexact=email)
    )
    if not candidates:
        return None
    exact_matches = [
        teacher
        for teacher in candidates
        if _same_text(teacher.first_name, first_name)
        and _same_text(teacher.last_name, last_name)
        and teacher.phone.strip() == phone.strip()
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        raise ValueError(
            "plusieurs enseignants identiques correspondent à ce courriel dans "
            "l'établissement"
        )
    raise ValueError(
        "les coordonnées de l'enseignant existant diffèrent du CSV ; "
        "aucune donnée existante n'a été modifiée"
    )


def _available_group_code(raw_code, seen_codes):
    raw_code = str(raw_code or "").strip()
    if not raw_code:
        raise ValueError("le code du groupe est obligatoire")
    if len(raw_code) > 80:
        raise ValueError("le code du groupe ne doit pas dépasser 80 caractères")
    code = normalize_group_code(raw_code)
    if not code:
        raise ValueError("le code du groupe n'est pas valide")
    if code in seen_codes:
        raise ValueError("ce code de groupe est dupliqué dans le fichier")
    if Registration.objects.filter(group_code=code).exists():
        raise ValueError("ce code de groupe existe déjà")
    return code


def _row_from_mapping(mapping, line, seen_codes):
    last_name = _text(
        mapping["nom_enseignant"], label="le nom de l'enseignant", max_length=100
    )
    first_name = _text(
        mapping["prenom_enseignant"],
        label="le prénom de l'enseignant",
        max_length=100,
    )
    email = _email(mapping["email_enseignant"])
    phone = _text(
        mapping["telephone_enseignant"],
        label="le téléphone de l'enseignant",
        max_length=30,
    )
    institution_name = _text(
        mapping["etablissement"], label="le nom de l'établissement", max_length=200
    )
    institution_type = _institution_type(mapping["type_etablissement"])
    institution_city = _text(mapping["commune"], label="la commune", max_length=120)
    institution_department = _department(mapping["departement"])
    family = _resolve_family(mapping["famille"])
    school_level = _resolve_school_level(mapping["niveau"])
    visit_date = _day(mapping["jour"])
    student_count = _integer(
        mapping["nb_etudiants"],
        label="le nombre d'étudiants",
        minimum=1,
        maximum=MAX_STUDENTS,
    )
    chaperone_count = _integer(
        mapping["nb_accompagnateurs"],
        label="le nombre d'accompagnateurs",
        minimum=0,
        maximum=MAX_CHAPERONES,
    )
    total_count = student_count + chaperone_count
    parsed_total = _integer(
        mapping["effectif_total"],
        label="l'effectif total",
        minimum=1,
        maximum=MAX_STUDENTS + MAX_CHAPERONES,
    )
    if parsed_total != total_count:
        raise ValueError(
            "l'effectif total doit être égal au nombre d'étudiants additionné "
            "au nombre d'accompagnateurs"
        )
    level_comment = _text(
        mapping.get("remarque_niveau", ""),
        label="la remarque sur le niveau",
        max_length=5_000,
        required=False,
    )
    comment = _text(
        mapping.get("remarque_generale", ""),
        label="la remarque générale",
        max_length=5_000,
        required=False,
    )
    group_code = _available_group_code(mapping.get("code_groupe", ""), seen_codes)

    institution = _find_institution(
        name=institution_name,
        city=institution_city,
        department=institution_department,
    )
    teacher = None
    if institution:
        _assert_institution_compatible(institution, institution_type)
        teacher = _find_teacher(
            institution=institution,
            email=email,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
        )

    return GroupImportRow(
        line=line,
        teacher_last_name=last_name,
        teacher_first_name=first_name,
        teacher_email=email,
        teacher_phone=phone,
        institution_name=institution_name,
        institution_type=institution_type,
        institution_city=institution_city,
        institution_department=institution_department,
        institution_id=institution.pk if institution else None,
        teacher_id=teacher.pk if teacher else None,
        group_code=group_code,
        family_id=family.pk,
        family_slug=family.slug,
        family_name=family.name,
        school_level_id=school_level.pk,
        school_level_code=school_level.code,
        school_level_label=school_level.label,
        visit_date=visit_date,
        student_count=student_count,
        chaperone_count=chaperone_count,
        total_count=total_count,
        level_comment=level_comment,
        comment=comment,
    )


def _file_identity(row):
    return (
        row.institution_name.casefold(),
        row.institution_city.casefold(),
        row.institution_department,
    )


def _file_teacher_identity(row):
    return (*_file_identity(row), row.teacher_email.casefold())


def _check_file_consistency(row, institution_rows, teacher_rows):
    institution_key = _file_identity(row)
    previous_institution = institution_rows.get(institution_key)
    if previous_institution and previous_institution.institution_type != row.institution_type:
        raise ValueError(
            "le même établissement possède plusieurs types dans le fichier"
        )
    institution_rows[institution_key] = row

    teacher_key = _file_teacher_identity(row)
    previous_teacher = teacher_rows.get(teacher_key)
    if previous_teacher and not (
        _same_text(previous_teacher.teacher_first_name, row.teacher_first_name)
        and _same_text(previous_teacher.teacher_last_name, row.teacher_last_name)
        and previous_teacher.teacher_phone == row.teacher_phone
    ):
        raise ValueError(
            "le même enseignant possède plusieurs coordonnées dans le fichier"
        )
    teacher_rows[teacher_key] = row


def preview_group_csv(upload):
    """Prévisualise un CSV de groupes sans effectuer aucune écriture."""
    preview = GroupImportPreview()
    try:
        content = _decode_csv(upload)
        reader = _csv_reader(content)
        header_mapping = _header_mapping(reader.fieldnames)
    except (AttributeError, csv.Error, ValueError) as error:
        preview.issues.append(GroupImportIssue(None, str(error)))
        return preview

    seen_codes = set()
    institution_rows = {}
    teacher_rows = {}
    source_row_count = 0
    for index, raw_row in enumerate(reader, start=2):
        if None in raw_row:
            preview.issues.append(
                GroupImportIssue(
                    index,
                    "La ligne contient des cellules supplémentaires ; vérifiez le "
                    "séparateur ou entourez les valeurs contenant une virgule de guillemets.",
                )
            )
            continue
        mapping = {column: "" for column in GROUP_IMPORT_COLUMNS}
        mapping.update(
            {
                canonical: str(raw_row.get(raw_header, "") or "")
                for raw_header, canonical in header_mapping.items()
            }
        )
        if not any(value.strip() for value in mapping.values()):
            continue
        source_row_count += 1
        if source_row_count > MAX_ROWS:
            preview.issues.append(
                GroupImportIssue(None, f"Le fichier ne doit pas dépasser {MAX_ROWS} lignes.")
            )
            break
        try:
            row = _row_from_mapping(mapping, index, seen_codes)
            _check_file_consistency(row, institution_rows, teacher_rows)
        except (KeyError, TypeError, ValueError) as error:
            preview.issues.append(GroupImportIssue(index, str(error)))
        else:
            seen_codes.add(row.group_code)
            preview.rows.append(row)

    if not preview.rows and not preview.issues:
        preview.issues.append(GroupImportIssue(None, "Le fichier ne contient aucun groupe."))
    return preview


def _payload_row(payload):
    if not isinstance(payload, dict):
        raise ValueError("une ligne de l'aperçu n'est pas valide")
    seal = payload.get("_seal")
    data = {key: value for key, value in payload.items() if key != "_seal"}
    try:
        signed_data = signing.loads(seal, salt=PAYLOAD_SALT)
    except (signing.BadSignature, TypeError) as error:
        raise ValueError("les données de l'aperçu ont été altérées") from error
    if signed_data != data:
        raise ValueError("les données de l'aperçu ont été altérées")

    try:
        payload_total = _integer(
            data["effectif_total"],
            label="l'effectif total",
            minimum=1,
            maximum=MAX_STUDENTS + MAX_CHAPERONES,
        )
        row = GroupImportRow(
            line=int(data["line"]),
            teacher_last_name=_text(
                data["teacher_last_name"],
                label="le nom de l'enseignant",
                max_length=100,
            ),
            teacher_first_name=_text(
                data["teacher_first_name"],
                label="le prénom de l'enseignant",
                max_length=100,
            ),
            teacher_email=_email(data["teacher_email"]),
            teacher_phone=_text(
                data["teacher_phone"],
                label="le téléphone de l'enseignant",
                max_length=30,
            ),
            institution_name=_text(
                data["institution_name"],
                label="le nom de l'établissement",
                max_length=200,
            ),
            institution_type=_institution_type(data["institution_type"]),
            institution_city=_text(
                data["institution_city"], label="la commune", max_length=120
            ),
            institution_department=_department(data["institution_department"]),
            institution_id=(
                int(data["institution_id"])
                if data["institution_id"] is not None
                else None
            ),
            teacher_id=int(data["teacher_id"]) if data["teacher_id"] is not None else None,
            group_code=normalize_group_code(data["group_code"]),
            family_id=int(data["family_id"]),
            family_slug=_text(data["family_slug"], label="la famille", max_length=120),
            family_name=_text(data["family_name"], label="la famille", max_length=100),
            school_level_id=int(data["school_level_id"]),
            school_level_code=_text(
                data["school_level_code"], label="le niveau", max_length=30
            ),
            school_level_label=_text(
                data["school_level_label"], label="le niveau", max_length=100
            ),
            visit_date=date.fromisoformat(str(data["visit_date"])),
            student_count=_integer(
                data["student_count"],
                label="le nombre d'étudiants",
                minimum=1,
                maximum=MAX_STUDENTS,
            ),
            chaperone_count=_integer(
                data["chaperone_count"],
                label="le nombre d'accompagnateurs",
                minimum=0,
                maximum=MAX_CHAPERONES,
            ),
            total_count=_integer(
                data["total_count"],
                label="l'effectif total",
                minimum=1,
                maximum=MAX_STUDENTS + MAX_CHAPERONES,
            ),
            level_comment=_text(
                data["level_comment"],
                label="la remarque sur le niveau",
                max_length=5_000,
                required=False,
            ),
            comment=_text(
                data["comment"],
                label="la remarque générale",
                max_length=5_000,
                required=False,
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("les données de l'aperçu ne sont plus valides") from error

    if not row.group_code:
        raise ValueError("le code du groupe n'est plus valide")
    if row.visit_date not in _event_dates():
        raise ValueError("la date ne correspond plus à un jour du salon")
    if row.total_count != row.student_count + row.chaperone_count:
        raise ValueError("l'effectif total de l'aperçu est incohérent")
    if payload_total != row.total_count:
        raise ValueError("l'effectif total de l'aperçu est incohérent")
    return row


def _locked_family(row):
    family = GroupFamily.objects.select_for_update().filter(pk=row.family_id).first()
    if (
        family is None
        or not family.is_active
        or family.slug != row.family_slug
        or family.name != row.family_name
    ):
        raise ValueError("la famille a été modifiée depuis la prévisualisation")
    return family


def _locked_school_level(row):
    level = SchoolLevel.objects.select_for_update().filter(pk=row.school_level_id).first()
    if (
        level is None
        or not level.is_active
        or level.code != row.school_level_code
        or level.label != row.school_level_label
    ):
        raise ValueError("le niveau a été modifié depuis la prévisualisation")
    return level


def _resolve_institution_for_import(row):
    institution = _find_institution(
        name=row.institution_name,
        city=row.institution_city,
        department=row.institution_department,
    )
    if row.institution_id is not None and (
        institution is None or institution.pk != row.institution_id
    ):
        raise ValueError(
            "l'établissement a été modifié depuis la prévisualisation"
        )
    if institution:
        _assert_institution_compatible(institution, row.institution_type)
        return institution
    return Institution.objects.create(
        name=row.institution_name,
        institution_type=row.institution_type,
        city=row.institution_city,
        department=row.institution_department,
    )


def _resolve_teacher_for_import(row, institution):
    teacher = _find_teacher(
        institution=institution,
        email=row.teacher_email,
        first_name=row.teacher_first_name,
        last_name=row.teacher_last_name,
        phone=row.teacher_phone,
    )
    if row.teacher_id is not None and (teacher is None or teacher.pk != row.teacher_id):
        raise ValueError("l'enseignant a été modifié depuis la prévisualisation")
    if teacher:
        return teacher
    return Teacher.objects.create(
        institution=institution,
        first_name=row.teacher_first_name,
        last_name=row.teacher_last_name,
        email=row.teacher_email,
        phone=row.teacher_phone,
    )


def _create_registration(row, *, institution, teacher, family, school_level, actor_user):
    access = create_draft(
        institution=institution,
        teacher=teacher,
        group_code=row.group_code,
        group_name=row.group_code,
        family=family,
        school_level=school_level,
        student_count=row.student_count,
        chaperone_count=row.chaperone_count,
        visit_date=row.visit_date,
        level_comment=row.level_comment,
        comment=row.comment,
        reservation_requests=(),
        actor_kind=RegistrationEvent.ActorKind.STAFF,
        actor_user=actor_user,
    )
    return access.registration


def import_group_payload(payload_rows, *, actor_user):
    """Revalide puis importe atomiquement un aperçu de groupes scellé."""
    if (
        not isinstance(payload_rows, list)
        or not payload_rows
        or len(payload_rows) > MAX_ROWS
    ):
        raise GroupImportError([GroupImportIssue(None, "L'aperçu n'est pas valide.")])
    if (
        actor_user is None
        or not getattr(actor_user, "pk", None)
        or not getattr(actor_user, "is_staff", False)
    ):
        raise GroupImportError(
            [
                GroupImportIssue(
                    None,
                    "L'utilisateur ayant lancé l'import doit appartenir à l'équipe.",
                )
            ]
        )

    issues = []
    rows = []
    seen_codes = set()
    institution_rows = {}
    teacher_rows = {}
    for payload in payload_rows:
        try:
            row = _payload_row(payload)
            if row.group_code in seen_codes:
                raise ValueError("ce code de groupe est dupliqué dans l'aperçu")
            _check_file_consistency(row, institution_rows, teacher_rows)
        except (TypeError, ValueError) as error:
            line = payload.get("line") if isinstance(payload, dict) else None
            issues.append(GroupImportIssue(line, str(error)))
        else:
            seen_codes.add(row.group_code)
            rows.append(row)
    if issues:
        raise GroupImportError(issues)

    created = []
    try:
        with transaction.atomic():
            # Tous les imports prennent les mêmes verrous dans le même ordre.
            # Sous PostgreSQL, cela sérialise les confirmations et empêche deux
            # transactions de créer simultanément les mêmes contacts absents.
            list(
                GroupFamily.objects.select_for_update()
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            existing_codes = set(
                Registration.objects.select_for_update()
                .filter(group_code__in=seen_codes)
                .values_list("group_code", flat=True)
            )
            if existing_codes:
                raise GroupImportError(
                    [
                        GroupImportIssue(
                            next(row.line for row in rows if row.group_code in existing_codes),
                            "ce code de groupe existe déjà",
                        )
                    ]
                )

            institutions = {}
            teachers = {}
            for row in rows:
                try:
                    family = _locked_family(row)
                    school_level = _locked_school_level(row)
                    institution_key = _file_identity(row)
                    institution = institutions.get(institution_key)
                    if institution is None:
                        institution = _resolve_institution_for_import(row)
                        institutions[institution_key] = institution
                    teacher_key = _file_teacher_identity(row)
                    teacher = teachers.get(teacher_key)
                    if teacher is None:
                        teacher = _resolve_teacher_for_import(row, institution)
                        teachers[teacher_key] = teacher
                    created.append(
                        _create_registration(
                            row,
                            institution=institution,
                            teacher=teacher,
                            family=family,
                            school_level=school_level,
                            actor_user=actor_user,
                        )
                    )
                except (RegistrationError, ValidationError, ValueError) as error:
                    raise GroupImportError(
                        [GroupImportIssue(row.line, str(error))]
                    ) from error
    except IntegrityError as error:
        raise GroupImportError(
            [
                GroupImportIssue(
                    None,
                    "Un groupe identique vient d'être créé. Rechargez l'aperçu.",
                )
            ]
        ) from error
    return created
