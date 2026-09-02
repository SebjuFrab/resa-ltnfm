"""Lecture et normalisation du classeur Excel d'import des groupes.

Le lecteur XLSX repose uniquement sur la bibliothèque standard afin que la
commande reste utilisable dans l'image Docker de production sans dépendance
supplémentaire.
"""

from __future__ import annotations

import csv
import posixpath
import re
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from xml.etree import ElementTree

from operations.group_imports import GROUP_IMPORT_COLUMNS, MAX_ROWS

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOCUMENT_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_WORKBOOK_SIZE = 20 * 1024 * 1024


class XlsxGroupImportError(ValueError):
    """Erreur lisible par l'opérateur lors de la préparation du classeur."""


@dataclass(frozen=True, slots=True)
class PreparedGroupWorkbook:
    csv_content: bytes
    group_count: int
    level_counts: dict[str, int]
    level_labels: dict[str, str]
    realigned_row_count: int
    harmonized_contact_count: int
    realigned_lines: tuple[int, ...]
    harmonized_contact_lines: tuple[int, ...]


def _normalize(value):
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


def _column_index(reference):
    letters = re.match(r"[A-Z]+", str(reference or "").upper())
    if not letters:
        raise XlsxGroupImportError("Une cellule du classeur n'a pas de référence valide.")
    index = 0
    for character in letters.group(0):
        index = index * 26 + ord(character) - ord("A") + 1
    return index - 1


def _archive_path(target):
    normalized = posixpath.normpath(posixpath.join("xl", target.lstrip("/")))
    if target.startswith("/xl/"):
        normalized = posixpath.normpath(target.lstrip("/"))
    if not normalized.startswith("xl/") or ".." in normalized.split("/"):
        raise XlsxGroupImportError("Le chemin interne d'une feuille XLSX est invalide.")
    return normalized


def _shared_strings(archive):
    try:
        root = ElementTree.parse(archive.open("xl/sharedStrings.xml")).getroot()
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t"))
        for item in root.findall(f"{{{MAIN_NS}}}si")
    ]


def _sheet_paths(archive):
    try:
        workbook = ElementTree.parse(archive.open("xl/workbook.xml")).getroot()
        relationships = ElementTree.parse(archive.open("xl/_rels/workbook.xml.rels")).getroot()
    except (ElementTree.ParseError, KeyError) as error:
        raise XlsxGroupImportError("Le fichier XLSX est incomplet ou endommagé.") from error

    targets = {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
        if relationship.attrib.get("Id") and relationship.attrib.get("Target")
    }
    paths = {}
    for sheet in workbook.findall(f".//{{{MAIN_NS}}}sheet"):
        relationship_id = sheet.attrib.get(f"{{{DOCUMENT_REL_NS}}}id")
        target = targets.get(relationship_id)
        if target and "worksheet" in target:
            paths[sheet.attrib["name"]] = _archive_path(target)
    return paths


def _cell_value(cell, shared_strings):
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{MAIN_NS}}}t"))
    value_node = cell.find(f"{{{MAIN_NS}}}v")
    value = value_node.text if value_node is not None and value_node.text else ""
    if cell_type == "s" and value:
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError) as error:
            raise XlsxGroupImportError(
                "Le tableau des textes partagés du classeur est invalide."
            ) from error
    if cell_type == "b":
        return "1" if value == "1" else "0"
    return value


def _sheet_matrix(archive, path, shared_strings):
    try:
        root = ElementTree.parse(archive.open(path)).getroot()
    except (ElementTree.ParseError, KeyError) as error:
        raise XlsxGroupImportError("Une feuille XLSX est illisible.") from error

    rows = []
    expected_row = 1
    for row_node in root.findall(f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row"):
        row_number = int(row_node.attrib.get("r", expected_row))
        while expected_row < row_number:
            rows.append([])
            expected_row += 1
        cells = {}
        for cell in row_node.findall(f"{{{MAIN_NS}}}c"):
            cells[_column_index(cell.attrib.get("r"))] = _cell_value(cell, shared_strings)
        width = max(cells, default=-1) + 1
        rows.append([cells.get(index, "") for index in range(width)])
        expected_row = row_number + 1
    return rows


def read_workbook_sheets(path, sheet_names):
    workbook_path = Path(path)
    if not workbook_path.is_file():
        raise XlsxGroupImportError(f"Fichier introuvable : {workbook_path}")
    if workbook_path.suffix.lower() != ".xlsx":
        raise XlsxGroupImportError("Le fichier doit être au format .xlsx.")
    if workbook_path.stat().st_size > MAX_WORKBOOK_SIZE:
        raise XlsxGroupImportError("Le classeur ne doit pas dépasser 20 Mo.")

    try:
        with zipfile.ZipFile(workbook_path) as archive:
            shared_strings = _shared_strings(archive)
            available_paths = _sheet_paths(archive)
            missing = [name for name in sheet_names if name not in available_paths]
            if missing:
                available = ", ".join(available_paths) or "aucune"
                raise XlsxGroupImportError(
                    f"Feuille absente : {', '.join(missing)}. Feuilles disponibles : {available}."
                )
            return {
                name: _sheet_matrix(archive, available_paths[name], shared_strings)
                for name in sheet_names
            }
    except zipfile.BadZipFile as error:
        raise XlsxGroupImportError("Le fichier n'est pas un classeur XLSX valide.") from error


def _level_catalog(rows):
    labels = {}
    normalized_to_code = {}
    for row_number, row in enumerate(rows, start=1):
        label = str(row[0] if row else "").strip()
        code = str(row[1] if len(row) > 1 else "").strip()
        if not label and not code:
            continue
        if not label or not code:
            raise XlsxGroupImportError(
                f"Onglet niveau, ligne {row_number} : libellé ou code manquant."
            )
        for value in (label, code):
            key = _normalize(value)
            previous = normalized_to_code.get(key)
            if previous and previous != code:
                raise XlsxGroupImportError(
                    f"Onglet niveau : la valeur {value!r} correspond à plusieurs codes."
                )
            normalized_to_code[key] = code
        labels[code] = label
    if not labels:
        raise XlsxGroupImportError("L'onglet niveau ne contient aucun niveau.")
    return normalized_to_code, labels


def _has_first_year(value):
    return bool(
        re.search(r"(?:^|_)(?:1|1ere|1re|premiere)(?:_|$)", value) or re.search(r"[a-z]1$", value)
    )


def _has_second_year(value):
    return bool(
        re.search(r"(?:^|_)(?:2|2e|2eme|2ieme|deuxieme)(?:_|$)", value)
        or re.search(r"[a-z]2$", value)
    )


def _mapped_level_code(raw_level, exact_codes):
    value = _normalize(raw_level)
    if not value:
        return exact_codes.get("non_renseigne", "NON_RENSEIGNE")
    if value in exact_codes:
        return exact_codes[value]

    is_bts = "bts" in value
    first_year = _has_first_year(value)
    second_year = _has_second_year(value)
    if is_bts:
        if first_year and not second_year:
            return "BTS_BTSA_1"
        if second_year and not first_year:
            return "BTS_BTSA_2"
        return "AUTRE NIVEAU"
    if value == "bprea" or value.startswith("bprea_"):
        return "BPREA"
    if "seconde" in value:
        return "LYC_2DE"
    if first_year and ("cgea" in value or "conduite" in value):
        return "LYC_1ERE_CGEA"
    if first_year and "stav" in value:
        return "LYC_1ERE_STAV"
    if (value.startswith("term_") or "terminale" in value) and "cgea" in value:
        return "LYC_TERM_CGEA"
    if (value.startswith("term_") or "terminale" in value) and "stav" in value:
        return "LYC_TERM_STAV"
    if value.startswith("cap") or value.startswith("bac_") or "terminale" in value:
        return "LYC_AUTRE"
    if "licence" in value or "bachelor" in value:
        return "SUP_LICENCE_BUT"
    if "master" in value or "ingenieur" in value:
        return "SUP_MASTER_ING"
    return "AUTRE NIVEAU"


def _group_rows(matrix):
    if not matrix:
        raise XlsxGroupImportError("La feuille des groupes est vide.")
    headers = [str(value or "").strip() for value in matrix[0]]
    if not any(headers):
        raise XlsxGroupImportError("La feuille des groupes n'a pas d'en-têtes.")
    duplicated = [header for header, count in Counter(headers).items() if header and count > 1]
    if duplicated:
        raise XlsxGroupImportError(
            f"En-têtes en double dans la feuille des groupes : {', '.join(duplicated)}."
        )
    rows = []
    for values in matrix[1:]:
        row = {
            header: str(values[index] if index < len(values) else "").strip()
            for index, header in enumerate(headers)
            if header
        }
        rows.append(row)
    return rows


def _family_from_institution_type(value):
    normalized = _normalize(value)
    if normalized in {"agricultural", "agricole", "etablissement_agricole"}:
        return "lycee-agricole"
    if normalized in {
        "higher_education",
        "enseignement_superieur",
        "superieur",
    }:
        return "enseignement-superieur"
    return "autre-public"


def _level_comment(existing_comment, raw_level):
    existing_comment = str(existing_comment or "").strip()
    raw_level = str(raw_level or "").strip()
    if not raw_level:
        return existing_comment
    detail = f"Niveau d'origine : {raw_level}"
    if not existing_comment:
        return detail
    return f"{existing_comment}\n{detail}"


def _looks_like_date(value):
    return bool(
        re.fullmatch(
            r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})",
            str(value or "").strip(),
        )
    )


def _realign_group_row(row):
    """Répare une ligne où des précisions décalent date et effectifs."""
    sequence_keys = (
        "jour",
        "nb_etudiants",
        "nb_accompagnateurs",
        "effectif_total",
        "remarque_niveau",
    )
    sequence = [str(row.get(key, "") or "").strip() for key in sequence_keys]
    date_index = next(
        (index for index, value in enumerate(sequence) if _looks_like_date(value)),
        None,
    )
    if date_index in (None, 0):
        return row, False

    details = [value for value in sequence[:date_index] if value]
    counts = sequence[date_index + 1 :]
    repaired = dict(row)
    repaired["jour"] = sequence[date_index]
    repaired["nb_etudiants"] = counts[0] if counts else ""
    repaired["nb_accompagnateurs"] = counts[1] if len(counts) > 1 else ""
    repaired["effectif_total"] = counts[2] if len(counts) > 2 else ""
    repaired["remarque_niveau"] = " — ".join(details)
    return repaired, True


def _harmonize_contact(row, known_contacts):
    institution_key = _normalize(row.get("etablissement"))
    email_key = _normalize(row.get("email_enseignant"))
    if not institution_key or not email_key:
        return row, False
    contact_key = (institution_key, email_key)
    contact_fields = (
        "nom_enseignant",
        "prenom_enseignant",
        "telephone_enseignant",
    )
    previous = known_contacts.get(contact_key)
    if previous is None:
        known_contacts[contact_key] = {field: row.get(field, "") for field in contact_fields}
        return row, False
    if all(
        _normalize(row.get(field)) == _normalize(previous.get(field)) for field in contact_fields
    ):
        return row, False
    harmonized = dict(row)
    harmonized.update(previous)
    return harmonized, True


def prepare_group_workbook(
    path,
    *,
    group_sheet="import groupe 2",
    level_sheet="niveau",
    keep_location=False,
):
    """Transforme le classeur métier en CSV normalisé pour l'import existant."""
    sheets = read_workbook_sheets(path, (group_sheet, level_sheet))
    exact_codes, level_labels = _level_catalog(sheets[level_sheet])
    rows = _group_rows(sheets[group_sheet])

    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=GROUP_IMPORT_COLUMNS, delimiter=";")
    writer.writeheader()
    level_counts = Counter()
    group_count = 0
    missing_codes = {}
    realigned_row_count = 0
    harmonized_contact_count = 0
    realigned_lines = []
    harmonized_contact_lines = []
    known_contacts = {}
    for source_line, row in enumerate(rows, start=2):
        if not any(str(value or "").strip() for value in row.values()):
            writer.writerow({column: "" for column in GROUP_IMPORT_COLUMNS})
            continue
        if group_count >= MAX_ROWS:
            raise XlsxGroupImportError(f"Le classeur ne doit pas dépasser {MAX_ROWS} groupes.")
        row, was_realigned = _realign_group_row(row)
        realigned_row_count += int(was_realigned)
        if was_realigned:
            realigned_lines.append(source_line)
        row, was_harmonized = _harmonize_contact(row, known_contacts)
        harmonized_contact_count += int(was_harmonized)
        if was_harmonized:
            harmonized_contact_lines.append(source_line)
        raw_level = row.get("niveau", "")
        level_code = _mapped_level_code(raw_level, exact_codes)
        if level_code not in level_labels:
            missing_codes.setdefault(level_code, []).append((source_line, raw_level))
        normalized = {column: row.get(column, "") for column in GROUP_IMPORT_COLUMNS}
        normalized["niveau"] = level_code
        normalized["remarque_niveau"] = _level_comment(normalized["remarque_niveau"], raw_level)
        if not normalized["famille"]:
            normalized["famille"] = _family_from_institution_type(normalized["type_etablissement"])
        # La localisation n'est pas utilisée pour cet import. Le type du
        # groupe est conservé via sa famille ; l'établissement reste neutre
        # pour éviter les conflits quand un campus accueille lycée et BTS.
        normalized["type_etablissement"] = ""
        if not keep_location:
            normalized["commune"] = ""
            normalized["departement"] = ""
        writer.writerow(normalized)
        level_counts[level_code] += 1
        group_count += 1

    if missing_codes:
        details = "; ".join(
            f"{code} (ex. ligne {items[0][0]} : {items[0][1] or 'vide'})"
            for code, items in sorted(missing_codes.items())
        )
        raise XlsxGroupImportError(
            "Ces niveaux sont nécessaires mais absents de l'onglet niveau : " + details
        )
    if not group_count:
        raise XlsxGroupImportError("La feuille des groupes ne contient aucun groupe.")

    return PreparedGroupWorkbook(
        csv_content=output.getvalue().encode("utf-8-sig"),
        group_count=group_count,
        level_counts=dict(sorted(level_counts.items())),
        level_labels=level_labels,
        realigned_row_count=realigned_row_count,
        harmonized_contact_count=harmonized_contact_count,
        realigned_lines=tuple(realigned_lines),
        harmonized_contact_lines=tuple(harmonized_contact_lines),
    )
