import csv
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from .factories import create_operational_data


class OperationsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = create_operational_data(institution_name="=Lycée des Champs")
        cls.staff = get_user_model().objects.create_user(
            username="staff", password="secret", is_staff=True
        )
        cls.staff.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label__in=("catalogue", "inscriptions"),
                codename__in=(
                    "view_session",
                    "view_institution",
                    "view_teacher",
                    "view_registration",
                    "view_reservation",
                ),
            )
        )

    def test_dashboard_is_staff_only(self):
        response = self.client.get(reverse("operations:dashboard"))
        self.assertEqual(response.status_code, 302)

        self.client.force_login(self.staff)
        response = self.client.get(reverse("operations:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["group_count"], 1)
        self.assertEqual(response.context["institution_count"], 1)
        self.assertEqual(response.context["global_fill_rate"], 86.7)
        self.assertNotContains(response, reverse("operations:registration-create"))

    def test_staff_without_business_permissions_is_denied(self):
        unprivileged = get_user_model().objects.create_user(
            username="unprivileged", password="secret", is_staff=True
        )
        self.client.force_login(unprivileged)
        self.assertEqual(
            self.client.get(reverse("operations:dashboard")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("operations:export-registrations")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("operations:final-reports")).status_code,
            403,
        )

    def test_add_registration_permission_alone_cannot_start_the_workflow(self):
        partial_staff = get_user_model().objects.create_user(
            username="partial", password="secret", is_staff=True
        )
        partial_staff.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="inscriptions",
                codename="add_registration",
            )
        )
        self.client.force_login(partial_staff)

        response = self.client.get(reverse("operations:registration-create"))

        self.assertEqual(response.status_code, 403)

    def test_registration_export_has_bom_expected_data_and_no_token(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("operations:export-registrations"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        decoded = response.content.decode("utf-8-sig")
        rows = list(csv.reader(StringIO(decoded), delimiter=";"))
        self.assertEqual(rows[0][0], "Établissement")
        self.assertEqual(rows[1][0], "'=Lycée des Champs")
        self.assertIn("Code groupe", rows[0])
        self.assertIn("Effectif total", rows[0])
        self.assertIn(self.data["registration"].group_code, rows[1])
        self.assertIn("26", rows[1])
        self.assertIn("marie@example.test", rows[1])
        self.assertNotIn("b" * 64, decoded)

    def test_reservation_and_session_exports_contain_the_group(self):
        self.client.force_login(self.staff)

        reservation_response = self.client.get(
            reverse("operations:export-reservations")
        )
        session_response = self.client.get(reverse("operations:export-sessions"))

        self.assertIn("Seconde A", reservation_response.content.decode("utf-8-sig"))
        self.assertIn(
            "Effectif total", reservation_response.content.decode("utf-8-sig")
        )
        session_content = session_response.content.decode("utf-8-sig")
        self.assertIn("Seconde A", session_content)
        self.assertIn("Effectif du groupe", session_content)
        self.assertIn("Capacité restante", session_content)

    def test_export_selection_can_use_a_comma_separator(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("operations:export-download"),
            {"export_type": "registrations", "delimiter": "comma"},
        )

        first_line = response.content.decode("utf-8-sig").splitlines()[0]
        self.assertIn(",", first_line)
        self.assertNotIn(";", first_line)

    def test_final_report_is_dense_printable_and_grouped_by_location(self):
        registration = self.data["registration"]
        registration.level_comment = "Niveau d'origine : classe mixte"
        registration.comment = "Arrivée par l'entrée nord"
        registration.save(update_fields=("level_comment", "comment", "updated_at"))
        session = self.data["session"]
        session.organizer = "Responsable du pôle"
        session.organizer_email = "pole@example.test"
        session.save(update_fields=("organizer", "organizer_email", "updated_at"))
        self.client.force_login(self.staff)

        response = self.client.get(reverse("operations:final-reports"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Récapitulatif général")
        self.assertContains(response, "Pôle sols")
        self.assertContains(response, registration.group_code)
        self.assertContains(response, "classe mixte")
        self.assertContains(response, "entrée nord")
        self.assertContains(response, "pole@example.test")
        self.assertContains(response, "Imprimer / enregistrer en PDF")
        self.assertEqual(response.context["location_count"], 1)
        self.assertEqual(response.context["group_count"], 1)
        self.assertEqual(response.context["participant_count"], 26)
        self.assertEqual(response.context["reservation_count"], 1)

    def test_final_report_can_be_filtered_by_location(self):
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("operations:final-reports"),
            {"date": "2026-09-23", "location": "Pôle sols"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["report_location"], "Pôle sols")
        self.assertEqual(response.context["report_date"].isoformat(), "2026-09-23")
        self.assertContains(response, "Récapitulatif — Pôle sols")
