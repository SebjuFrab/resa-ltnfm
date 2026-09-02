import csv
from datetime import date
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from catalogue.models import SchoolLevel
from inscriptions.models import (
    GroupFamily,
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
    Teacher,
)
from operations.group_imports import (
    GROUP_IMPORT_COLUMNS,
    GroupImportError,
    import_group_payload,
    preview_group_csv,
)


@override_settings(EVENT_DATES=("2026-09-23", "2026-09-24"))
class GroupImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = get_user_model().objects.create_user(
            username="group-importer",
            password="secret",
            is_staff=True,
        )
        cls.family = GroupFamily.objects.create(
            name="Famille test",
            slug="famille-test",
            sort_order=10,
        )
        cls.level = SchoolLevel.objects.create(
            code="IMPORT_TEST",
            label="Niveau import test",
            sort_order=10,
        )

    def _row(self, **overrides):
        row = {
            "nom_enseignant": "Martin",
            "prenom_enseignant": "Alice",
            "email_enseignant": "alice@example.test",
            "telephone_enseignant": "06 00 00 00 01",
            "etablissement": "Lycée du Bocage",
            "type_etablissement": "lycée agricole",
            "commune": "Rennes",
            "departement": "35",
            "code_groupe": "carotte-doree",
            "famille": self.family.slug,
            "niveau": self.level.code,
            "jour": "23/09/2026",
            "nb_etudiants": "24",
            "nb_accompagnateurs": "2",
            "effectif_total": "26",
            "remarque_niveau": "Classe mixte",
            "remarque_generale": "Arrivée en car",
        }
        row.update(overrides)
        return row

    def _upload(
        self,
        rows,
        *,
        columns=GROUP_IMPORT_COLUMNS,
        delimiter=";",
        encoding="utf-8",
    ):
        stream = StringIO(newline="")
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(columns)
        for row in rows:
            if isinstance(row, dict):
                writer.writerow([row.get(column, "") for column in columns])
            else:
                writer.writerow(row)
        return SimpleUploadedFile(
            "groupes.csv",
            stream.getvalue().encode(encoding),
            content_type="text/csv",
        )

    def test_preview_is_template_compatible_and_performs_no_write(self):
        before = {
            model: model.objects.count()
            for model in (Institution, Teacher, Registration, RegistrationEvent)
        }

        preview = preview_group_csv(self._upload([self._row()]))

        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(len(preview.rows), 1)
        row = preview.rows[0]
        self.assertEqual(row.line, 2)
        self.assertEqual(row.total_count, 26)
        self.assertEqual(row.effectif_total, 26)
        self.assertEqual(row.family_id, self.family.pk)
        self.assertEqual(row.school_level_id, self.level.pk)
        self.assertEqual(row.institution_type, Institution.Type.AGRICULTURAL)
        self.assertEqual(row.as_payload()["effectif_total"], 26)
        self.assertEqual(
            before,
            {
                model: model.objects.count()
                for model in (Institution, Teacher, Registration, RegistrationEvent)
            },
        )

    def test_import_creates_draft_contacts_and_staff_audit_without_email_or_booking(self):
        preview = preview_group_csv(self._upload([self._row()]))
        self.assertTrue(preview.is_valid, preview.issues)
        group_code = preview.rows[0].group_code

        created = import_group_payload(
            [preview.rows[0].as_payload()], actor_user=self.actor
        )

        self.assertEqual(len(created), 1)
        registration = Registration.objects.get(pk=created[0].pk)
        self.assertEqual(registration.status, Registration.Status.DRAFT)
        self.assertIsNotNone(registration.draft_expires_at)
        self.assertIsNone(registration.confirmed_at)
        self.assertEqual(registration.group_code, group_code)
        self.assertEqual(registration.group_name, group_code)
        self.assertEqual(registration.student_count, 24)
        self.assertEqual(registration.chaperone_count, 2)
        self.assertEqual(registration.total_participant_count, 26)
        self.assertEqual(registration.family, self.family)
        self.assertEqual(registration.school_level, self.level)
        self.assertEqual(registration.level_comment, "Classe mixte")
        self.assertEqual(registration.comment, "Arrivée en car")
        self.assertEqual(registration.institution.institution_type, Institution.Type.AGRICULTURAL)
        self.assertEqual(registration.teacher.email, "alice@example.test")
        self.assertFalse(Reservation.objects.filter(registration=registration).exists())
        event = registration.events.get()
        self.assertEqual(event.event_type, RegistrationEvent.Type.CREATED)
        self.assertEqual(event.actor_kind, RegistrationEvent.ActorKind.STAFF)
        self.assertEqual(event.actor_user, self.actor)
        self.assertEqual(mail.outbox, [])

    def test_exact_institution_and_teacher_are_reused_without_modification(self):
        institution = Institution.objects.create(
            name="Lycée du Bocage",
            institution_type=Institution.Type.AGRICULTURAL,
            city="Rennes",
            department="35",
            address="Adresse conservée",
        )
        teacher = Teacher.objects.create(
            institution=institution,
            first_name="Alice",
            last_name="Martin",
            email="Alice@Example.Test",
            phone="06 00 00 00 01",
        )
        preview = preview_group_csv(self._upload([self._row()]))

        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(preview.rows[0].institution_id, institution.pk)
        self.assertEqual(preview.rows[0].teacher_id, teacher.pk)
        created = import_group_payload(
            [preview.rows[0].as_payload()], actor_user=self.actor
        )

        self.assertEqual(created[0].institution_id, institution.pk)
        self.assertEqual(created[0].teacher_id, teacher.pk)
        self.assertEqual(Institution.objects.count(), 1)
        self.assertEqual(Teacher.objects.count(), 1)
        institution.refresh_from_db()
        teacher.refresh_from_db()
        self.assertEqual(institution.address, "Adresse conservée")
        self.assertEqual(teacher.email, "Alice@Example.Test")

    def test_existing_contact_difference_is_rejected_without_silent_update(self):
        institution = Institution.objects.create(
            name="Lycée du Bocage",
            institution_type=Institution.Type.AGRICULTURAL,
            city="Rennes",
            department="35",
        )
        teacher = Teacher.objects.create(
            institution=institution,
            first_name="Alice",
            last_name="Martin",
            email="alice@example.test",
            phone="02 99 00 00 00",
        )

        preview = preview_group_csv(self._upload([self._row()]))

        self.assertFalse(preview.is_valid)
        self.assertIn("diffèrent", preview.issues[0].message)
        teacher.refresh_from_db()
        self.assertEqual(teacher.phone, "02 99 00 00 00")
        self.assertEqual(Registration.objects.count(), 0)

    def test_effectif_total_is_optional_and_must_match_when_provided(self):
        missing = preview_group_csv(
            self._upload([self._row(effectif_total="")])
        )
        mismatch = preview_group_csv(
            self._upload([self._row(effectif_total="25")])
        )

        self.assertTrue(missing.is_valid, missing.issues)
        self.assertEqual(missing.rows[0].total_count, 26)
        self.assertFalse(mismatch.is_valid)
        self.assertIn("additionné", mismatch.issues[0].message)

    def test_group_code_is_generated_when_missing_and_too_long_values_are_rejected(self):
        missing = preview_group_csv(self._upload([self._row(code_groupe="")]))
        too_long = preview_group_csv(
            self._upload([self._row(code_groupe="a" * 81)])
        )

        self.assertTrue(missing.is_valid, missing.issues)
        self.assertTrue(missing.rows[0].group_code)
        self.assertFalse(too_long.is_valid)
        self.assertIn("80", too_long.issues[0].message)

    def test_subset_of_columns_and_blank_values_use_safe_defaults(self):
        preview = preview_group_csv(
            self._upload(
                [{"etablissement": "Structure sans autres informations"}],
                columns=("etablissement",),
            )
        )

        self.assertTrue(preview.is_valid, preview.issues)
        row = preview.rows[0]
        self.assertEqual(row.institution_name, "Structure sans autres informations")
        self.assertEqual(row.institution_type, Institution.Type.OTHER)
        self.assertEqual(row.institution_city, "")
        self.assertEqual(row.institution_department, "")
        self.assertEqual(row.teacher_email, "")
        self.assertTrue(row.teacher_last_name.startswith("À compléter"))
        self.assertEqual(row.family_slug, "autre-public")
        self.assertEqual(row.school_level_code, "NON_RENSEIGNE")
        self.assertEqual(row.visit_date, date(2026, 9, 23))
        self.assertEqual(row.student_count, 1)
        self.assertEqual(row.chaperone_count, 0)
        self.assertEqual(row.total_count, 1)

        try:
            created = import_group_payload([row.as_payload()], actor_user=self.actor)
        except GroupImportError as error:
            self.fail(error.issues)

        registration = created[0]
        self.assertEqual(registration.teacher.email, "")
        self.assertEqual(registration.student_count, 1)
        self.assertEqual(registration.school_level.code, "NON_RENSEIGNE")

    def test_total_can_supply_missing_student_count(self):
        preview = preview_group_csv(
            self._upload(
                [{"effectif_total": "12", "nb_accompagnateurs": "2"}],
                columns=("effectif_total", "nb_accompagnateurs"),
            )
        )

        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(preview.rows[0].student_count, 10)
        self.assertEqual(preview.rows[0].chaperone_count, 2)
        self.assertEqual(preview.rows[0].total_count, 12)

    def test_alias_headers_cp1252_comma_and_human_values_are_accepted(self):
        columns = (
            "Nom professeur",
            "Prénom professeur",
            "Mail professeur",
            "Téléphone professeur",
            "Nom établissement",
            "Type structure",
            "Ville",
            "Dépt",
            "Nom code",
            "Catégorie",
            "Niveau scolaire",
            "Date visite",
            "Nombre élèves",
            "Nombre accompagnateurs",
            "Effectif",
            "Commentaire niveau",
            "Commentaire",
        )
        values = (
            "Martin",
            "Alice",
            "alice@example.test",
            "0600000001",
            "Lycée du Bocage",
            "agricole",
            "Rennes",
            "35 — Ille-et-Vilaine",
            "carotte-doree",
            "Famille test",
            "Niveau import test",
            "mercredi",
            "24",
            "2",
            "26",
            "Mixte",
            "RAS",
        )

        preview = preview_group_csv(
            self._upload(
                [values],
                columns=columns,
                delimiter=",",
                encoding="cp1252",
            )
        )

        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(preview.rows[0].visit_date.isoformat(), "2026-09-23")
        self.assertEqual(preview.rows[0].institution_department, "35")

    def test_inactive_unknown_and_ambiguous_references_are_reported(self):
        self.family.is_active = False
        self.family.save(update_fields=("is_active",))
        inactive = preview_group_csv(self._upload([self._row()]))
        self.assertFalse(inactive.is_valid)
        self.assertIn("inactive", inactive.issues[0].message)

        self.family.is_active = True
        self.family.save(update_fields=("is_active",))
        unknown = preview_group_csv(
            self._upload([self._row(niveau="NIVEAU_INCONNU")])
        )
        self.assertFalse(unknown.is_valid)
        self.assertIn("inconnu", unknown.issues[0].message)

        GroupFamily.objects.create(name="Premier nom", slug="famille-ambigue")
        GroupFamily.objects.create(name="Famille ambiguë", slug="second-slug")
        ambiguous = preview_group_csv(
            self._upload([self._row(famille="famille ambigue")])
        )
        self.assertFalse(ambiguous.is_valid)
        self.assertIn("ambiguë", ambiguous.issues[0].message)

    def test_file_and_database_group_code_duplicates_are_rejected(self):
        duplicate_file = preview_group_csv(
            self._upload(
                [
                    self._row(),
                    self._row(
                        email_enseignant="bob@example.test",
                        prenom_enseignant="Bob",
                    ),
                ]
            )
        )
        self.assertFalse(duplicate_file.is_valid)
        self.assertIn("dupliqué", duplicate_file.issues[0].message)

        institution = Institution.objects.create(
            name="Autre lycée",
            institution_type=Institution.Type.HIGH_SCHOOL,
            city="Vannes",
            department="56",
        )
        teacher = Teacher.objects.create(
            institution=institution,
            first_name="Jean",
            last_name="Durand",
            email="jean@example.test",
            phone="0600000002",
        )
        Registration.objects.create(
            institution=institution,
            teacher=teacher,
            group_code="carotte-doree",
            group_name="carotte-doree",
            family=self.family,
            school_level=self.level,
            student_count=10,
            chaperone_count=1,
            visit_date="2026-09-23",
            status=Registration.Status.CONFIRMED,
            edit_token_digest="a" * 64,
        )
        duplicate_database = preview_group_csv(self._upload([self._row()]))
        self.assertFalse(duplicate_database.is_valid)
        self.assertIn("existe déjà", duplicate_database.issues[0].message)

    def test_import_rejects_tampered_payload(self):
        preview = preview_group_csv(self._upload([self._row()]))
        self.assertTrue(preview.is_valid, preview.issues)
        payload = preview.rows[0].as_payload()
        payload["student_count"] = 25

        with self.assertRaises(GroupImportError) as caught:
            import_group_payload([payload], actor_user=self.actor)

        self.assertIn("altéré", caught.exception.issues[0].message)
        self.assertEqual(Registration.objects.count(), 0)

    def test_import_is_atomic_when_reference_changes_after_preview(self):
        second_level = SchoolLevel.objects.create(
            code="IMPORT_SECOND",
            label="Second niveau d'import",
            sort_order=20,
        )
        preview = preview_group_csv(
            self._upload(
                [
                    self._row(),
                    self._row(
                        nom_enseignant="Durand",
                        prenom_enseignant="Bob",
                        email_enseignant="bob@example.test",
                        telephone_enseignant="0600000002",
                        etablissement="Lycée maritime",
                        commune="Lorient",
                        departement="56",
                        code_groupe="poire-argentee",
                        niveau=second_level.code,
                    ),
                ]
            )
        )
        self.assertTrue(preview.is_valid, preview.issues)
        second_level.is_active = False
        second_level.save(update_fields=("is_active",))

        with self.assertRaises(GroupImportError) as caught:
            import_group_payload(
                [row.as_payload() for row in preview.rows], actor_user=self.actor
            )

        self.assertIn("modifié", caught.exception.issues[0].message)
        self.assertEqual(Institution.objects.count(), 0)
        self.assertEqual(Teacher.objects.count(), 0)
        self.assertEqual(Registration.objects.count(), 0)
        self.assertEqual(RegistrationEvent.objects.count(), 0)

    def test_department_area_and_staff_actor_are_enforced(self):
        invalid_department = preview_group_csv(
            self._upload([self._row(departement="75")])
        )
        self.assertFalse(invalid_department.is_valid)
        self.assertIn("22", invalid_department.issues[0].message)

        preview = preview_group_csv(self._upload([self._row()]))
        non_staff = get_user_model().objects.create_user(username="ordinary")
        with self.assertRaises(GroupImportError) as caught:
            import_group_payload(
                [preview.rows[0].as_payload()], actor_user=non_staff
            )
        self.assertIn("équipe", caught.exception.issues[0].message)
        self.assertEqual(Registration.objects.count(), 0)

    def test_more_than_500_rows_are_rejected(self):
        rows = [
            self._row(
                code_groupe=f"groupe-{index}",
                email_enseignant=f"prof{index}@example.test",
            )
            for index in range(501)
        ]

        preview = preview_group_csv(self._upload(rows))

        self.assertFalse(preview.is_valid)
        self.assertIn("500", preview.issues[-1].message)
        self.assertEqual(Registration.objects.count(), 0)
