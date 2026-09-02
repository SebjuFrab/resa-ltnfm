import csv
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from catalogue.models import Animation, SchoolLevel
from communication.models import EmailLog
from inscriptions.models import GroupFamily, Registration, Reservation
from operations.group_imports import preview_group_csv
from operations.permissions import REGISTRATION_MANAGE_PERMISSIONS


@override_settings(EVENT_DATES=("2026-09-23", "2026-09-24"))
class GroupImportViewTests(TestCase):
    HEADER = (
        "nom_enseignant;prenom_enseignant;email_enseignant;"
        "telephone_enseignant;etablissement;type_etablissement;commune;"
        "departement;code_groupe;famille;niveau;jour;nb_etudiants;"
        "nb_accompagnateurs;effectif_total;remarque_niveau;remarque_generale\n"
    )

    @classmethod
    def setUpTestData(cls):
        cls.family, _ = GroupFamily.objects.get_or_create(
            slug="lycee-agricole",
            defaults={"name": "Lycée agricole", "sort_order": 10},
        )
        cls.level, _ = SchoolLevel.objects.get_or_create(
            code="LYC_2DE",
            defaults={"label": "Seconde", "sort_order": 30},
        )
        cls.staff = get_user_model().objects.create_user(
            username="group-importer",
            password="secret",
            is_staff=True,
        )
        for permission_name in REGISTRATION_MANAGE_PERMISSIONS:
            app_label, codename = permission_name.split(".", 1)
            cls.staff.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label=app_label,
                    codename=codename,
                )
            )

    @classmethod
    def _upload(cls, row, *, name="groupes.csv"):
        return SimpleUploadedFile(
            name,
            (cls.HEADER + row).encode(),
            content_type="text/csv",
        )

    @staticmethod
    def _valid_row(code="chou-orange"):
        return (
            "Roux;Alice;alice.roux@example.test;06 01 02 03 04;"
            "Lycée agricole de Retiers;AGRICULTURAL;Retiers;35;"
            f"{code};lycee-agricole;LYC_2DE;2026-09-23;24;2;26;"
            "Classe de seconde;Arrivée à 9 h\n"
        )

    def test_import_and_template_require_staff_with_all_management_permissions(self):
        for route_name in ("operations:group-import", "operations:group-import-template"):
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 302)

        partial_staff = get_user_model().objects.create_user(
            username="partial-group-importer",
            password="secret",
            is_staff=True,
        )
        partial_staff.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="inscriptions",
                codename="add_registration",
            )
        )
        self.client.force_login(partial_staff)

        for route_name in ("operations:group-import", "operations:group-import-template"):
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 403)

    def test_page_links_to_template_and_is_available_from_navigation(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse("operations:group-import"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("operations:group-import-template"))
        self.assertContains(response, "Télécharger le modèle CSV")
        self.assertContains(response, "brouillon")
        self.assertContains(response, "Aucune colonne n’est obligatoire")

        dashboard = self.client.get(reverse("operations:dashboard"))
        self.assertContains(dashboard, reverse("operations:group-import"))

    def test_csv_template_has_bom_exact_columns_totals_and_valid_examples(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse("operations:group-import-template"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="modele_import_groupes.csv"',
        )
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        rows = list(
            csv.reader(
                StringIO(response.content.decode("utf-8-sig")),
                delimiter=";",
            )
        )
        self.assertEqual(rows[0], self.HEADER.rstrip().split(";"))
        self.assertEqual(len(rows), 3)
        for row in rows[1:]:
            self.assertEqual(int(row[14]), int(row[12]) + int(row[13]))

        preview = preview_group_csv(
            SimpleUploadedFile(
                "modele_import_groupes.csv",
                response.content,
                content_type="text/csv",
            )
        )
        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(len(preview.rows), 2)

    def test_preview_then_confirmation_creates_draft_without_booking_or_email(self):
        self.client.force_login(self.staff)
        animation_count = Animation.objects.count()

        response = self.client.post(
            reverse("operations:group-import"),
            {"file": self._upload(self._valid_row())},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aperçu")
        self.assertContains(response, "chou-orange")
        self.assertContains(response, "26")
        self.assertEqual(Registration.objects.count(), 0)

        response = self.client.post(
            reverse("operations:group-import"),
            {"action": "confirm"},
            follow=True,
        )

        registration = Registration.objects.get(group_code="chou-orange")
        registration_list_url = reverse("operations:registration-list")
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        self.assertRedirects(
            response,
            f"{registration_list_url}?status={Registration.Status.DRAFT}",
        )
        self.assertEqual(registration.status, Registration.Status.DRAFT)
        self.assertEqual(registration.total_participant_count, 26)
        self.assertEqual(Reservation.objects.count(), 0)
        self.assertEqual(Animation.objects.count(), animation_count)
        self.assertEqual(EmailLog.objects.count(), 0)
        self.assertContains(response, registration.group_code)
        self.assertContains(response, planning_url)
        self.assertContains(response, "Dernier import")
        self.assertContains(response, "Aucun courriel")

        planning_response = self.client.get(planning_url)
        self.assertEqual(planning_response.status_code, 200)
        self.assertContains(planning_response, registration.group_code)

    def test_cancel_discards_preview_without_creating_a_group(self):
        self.client.force_login(self.staff)
        self.client.post(
            reverse("operations:group-import"),
            {"file": self._upload(self._valid_row("poire-violette"))},
        )

        response = self.client.post(
            reverse("operations:group-import"),
            {"action": "cancel"},
        )

        self.assertRedirects(response, reverse("operations:group-import"))
        self.assertEqual(Registration.objects.count(), 0)

    def test_imported_draft_can_be_opened_and_saved_from_the_update_form(self):
        self.client.force_login(self.staff)
        self.client.post(
            reverse("operations:group-import"),
            {"file": self._upload(self._valid_row("carotte-bleue"))},
        )
        self.client.post(
            reverse("operations:group-import"),
            {"action": "confirm"},
        )
        registration = Registration.objects.select_related(
            "institution",
            "teacher",
        ).get(group_code="carotte-bleue")
        update_url = reverse(
            "operations:registration-update",
            kwargs={"reference": registration.reference},
        )

        response = self.client.get(update_url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            update_url,
            {
                "existing_institution": str(registration.institution_id),
                "institution_type": registration.institution.institution_type,
                "institution_name": "",
                "institution_city": "",
                "institution_department": "",
                "teacher_last_name": registration.teacher.last_name,
                "teacher_first_name": registration.teacher.first_name,
                "teacher_email": registration.teacher.email,
                "teacher_phone": registration.teacher.phone,
                "group_code": registration.group_code,
                "family": str(registration.family_id),
                "school_level": str(registration.school_level_id),
                "visit_date": registration.visit_date.isoformat(),
                "student_count": str(registration.student_count),
                "chaperone_count": str(registration.chaperone_count),
                "level_comment": registration.level_comment,
                "comment": registration.comment,
            },
        )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-review",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
