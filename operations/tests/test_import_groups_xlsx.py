import tempfile
import zipfile
from html import escape
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from catalogue.models import SchoolLevel
from inscriptions.models import Institution, Registration
from operations.xlsx_group_import import prepare_group_workbook

GROUP_HEADERS = (
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


def _column_letters(index):
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _worksheet_xml(rows):
    rendered_rows = []
    for row_number, row in enumerate(rows, start=1):
        rendered_cells = []
        for column, value in enumerate(row):
            if value in (None, ""):
                continue
            reference = f"{_column_letters(column)}{row_number}"
            rendered_cells.append(
                f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
            )
        rendered_rows.append(f'<row r="{row_number}">{"".join(rendered_cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        f"{''.join(rendered_rows)}</sheetData></worksheet>"
    )


def _create_workbook(path, group_rows, level_rows):
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="import groupe 2" sheetId="1" r:id="rId1"/>'
        '<sheet name="niveau" sheetId="2" r:id="rId2"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            _worksheet_xml((GROUP_HEADERS, *group_rows)),
        )
        archive.writestr("xl/worksheets/sheet2.xml", _worksheet_xml(level_rows))


class ImportGroupsXlsxCommandTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="importeur", is_staff=True)
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workbook_path = Path(self.temporary_directory.name) / "groupes.xlsx"
        self.level_rows = (
            ("BTS / BTSA — 1re année", "BTS_BTSA_1"),
            ("Autre niveau", "AUTRE NIVEAU"),
        )
        self.group_row = (
            "Martin",
            "Alice",
            "alice@example.test",
            "0600000000",
            "Campus test",
            "HIGHER_EDUCATION",
            "Rennes",
            "35",
            "groupe-test-xlsx",
            "",
            "BTS ACS AGRI 1",
            "23/09/2026",
            "24",
            "2",
            "26",
            "",
            "RAS",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_workbook(self):
        _create_workbook(
            self.workbook_path,
            (self.group_row,),
            self.level_rows,
        )

    def test_preparation_maps_level_and_preserves_original_detail(self):
        self._write_workbook()

        prepared = prepare_group_workbook(self.workbook_path)
        csv_text = prepared.csv_content.decode("utf-8-sig")

        self.assertEqual(prepared.group_count, 1)
        self.assertEqual(prepared.level_counts, {"BTS_BTSA_1": 1})
        self.assertIn("Niveau d'origine : BTS ACS AGRI 1", csv_text)
        self.assertIn("enseignement-superieur", csv_text)
        self.assertNotIn("Rennes", csv_text)

    def test_command_is_dry_run_by_default_then_imports_atomically(self):
        self._write_workbook()
        SchoolLevel.objects.update_or_create(
            code="BTS_BTSA_1",
            defaults={"label": "BTS / BTSA — 1re année", "is_active": True},
        )
        output = StringIO()

        call_command(
            "import_groups_xlsx",
            self.workbook_path,
            "--actor",
            self.actor.username,
            stdout=output,
        )

        self.assertIn("SIMULATION UNIQUEMENT", output.getvalue())
        self.assertEqual(Registration.objects.count(), 0)

        call_command(
            "import_groups_xlsx",
            self.workbook_path,
            "--actor",
            self.actor.username,
            "--commit",
            stdout=output,
        )

        registration = Registration.objects.get(group_code="groupe-test-xlsx")
        self.assertEqual(registration.school_level.code, "BTS_BTSA_1")
        self.assertEqual(registration.family.slug, "enseignement-superieur")
        self.assertEqual(registration.level_comment, "Niveau d'origine : BTS ACS AGRI 1")
        self.assertEqual(registration.institution.city, "")
        self.assertEqual(registration.institution.department, "")
        self.assertEqual(registration.institution.institution_type, Institution.Type.OTHER)
        self.assertIn("1 groupes créés", output.getvalue())

    def test_command_stops_when_a_workbook_level_is_missing_in_database(self):
        self._write_workbook()
        SchoolLevel.objects.filter(code="BTS_BTSA_1").delete()

        with self.assertRaisesMessage(CommandError, "BTS_BTSA_1"):
            call_command(
                "import_groups_xlsx",
                self.workbook_path,
                "--actor",
                self.actor.username,
            )

        self.assertEqual(Registration.objects.count(), 0)

    def test_preparation_repairs_shifted_values_and_harmonizes_contact(self):
        first_row = list(self.group_row)
        second_row = list(self.group_row)
        first_row[8] = "premier-groupe"
        second_row[8] = "second-groupe"
        second_row[3] = "0600000001"
        second_row[11] = "2e année"
        second_row[12] = "23/09/2026"
        second_row[13] = "12"
        second_row[14] = "2"
        second_row[15] = "14"
        _create_workbook(
            self.workbook_path,
            (tuple(first_row), tuple(second_row)),
            self.level_rows,
        )

        prepared = prepare_group_workbook(self.workbook_path)
        csv_text = prepared.csv_content.decode("utf-8-sig")

        self.assertEqual(prepared.realigned_row_count, 1)
        self.assertEqual(prepared.harmonized_contact_count, 1)
        self.assertEqual(prepared.realigned_lines, (3,))
        self.assertEqual(prepared.harmonized_contact_lines, (3,))
        self.assertIn("2e année", csv_text)
        self.assertNotIn("0600000001", csv_text)
