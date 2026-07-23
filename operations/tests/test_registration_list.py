from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalogue.models import SchoolLevel
from inscriptions.models import Institution, Registration, Teacher
from operations.permissions import REGISTRATION_MANAGE_PERMISSIONS


class RegistrationListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="registration-manager",
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

        cls.partial_staff = get_user_model().objects.create_user(
            username="registration-list-reader",
            password="secret",
            is_staff=True,
        )
        cls.partial_staff.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="inscriptions",
                codename="view_registration",
            )
        )

        cls.level = SchoolLevel.objects.create(code="LISTE_LYC", label="Lycée")
        cls.institution = Institution.objects.create(
            name="Lycée des Bruyères",
            institution_type=Institution.Type.HIGH_SCHOOL,
            city="Rennes",
            department="35",
        )
        cls.teacher = Teacher.objects.create(
            institution=cls.institution,
            first_name="Alice",
            last_name="Martin",
            email="alice.martin@example.test",
            phone="0601020304",
        )
        cls.draft = cls._registration(
            group_code="pomme-verte",
            group_name="Pomme verte",
            visit_date=date(2026, 9, 23),
            status=Registration.Status.DRAFT,
            edit_token_digest="a" * 64,
            draft_expires_at=timezone.now() + timedelta(hours=1),
        )
        cls.confirmed = cls._registration(
            group_code="poire-rouge",
            group_name="Poire rouge",
            visit_date=date(2026, 9, 24),
            status=Registration.Status.CONFIRMED,
            edit_token_digest="b" * 64,
            confirmed_at=timezone.now(),
        )
        cls.cancelled = cls._registration(
            group_code="navet-bleu",
            group_name="Navet bleu",
            visit_date=date(2026, 9, 23),
            status=Registration.Status.CANCELLED,
            edit_token_digest="c" * 64,
            cancelled_at=timezone.now(),
        )

    @classmethod
    def _registration(cls, **values):
        return Registration.objects.create(
            institution=cls.institution,
            teacher=cls.teacher,
            school_level=cls.level,
            student_count=24,
            chaperone_count=2,
            **values,
        )

    def test_list_requires_staff_with_all_registration_management_permissions(self):
        url = reverse("operations:registration-list")

        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        self.client.force_login(self.partial_staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_list_exposes_filters_and_imported_drafts(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse("operations:registration-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="q"')
        self.assertContains(response, 'name="date"')
        self.assertContains(response, 'name="status"')
        self.assertContains(response, self.draft.group_code)
        self.assertContains(response, Registration.Status(self.draft.status).label)
        self.assertContains(
            response,
            reverse(
                "operations:registration-planning",
                kwargs={"reference": self.draft.reference},
            ),
        )

    def test_list_filters_by_query_date_and_status(self):
        self.client.force_login(self.staff)
        url = reverse("operations:registration-list")

        query_response = self.client.get(url, {"q": "pomme-verte"})
        self.assertContains(query_response, self.draft.group_code)
        self.assertNotContains(query_response, self.confirmed.group_code)
        self.assertNotContains(query_response, self.cancelled.group_code)

        date_response = self.client.get(url, {"date": "2026-09-24"})
        self.assertNotContains(date_response, self.draft.group_code)
        self.assertContains(date_response, self.confirmed.group_code)
        self.assertNotContains(date_response, self.cancelled.group_code)

        status_response = self.client.get(
            url,
            {"status": Registration.Status.CANCELLED},
        )
        self.assertNotContains(status_response, self.draft.group_code)
        self.assertNotContains(status_response, self.confirmed.group_code)
        self.assertContains(status_response, self.cancelled.group_code)

    def test_actions_match_the_registration_status(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse("operations:registration-list"))

        draft_planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": self.draft.reference},
        )
        confirmed_planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": self.confirmed.reference},
        )
        cancelled_planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": self.cancelled.reference},
        )
        cancelled_detail_url = reverse(
            "operations:registration-detail",
            kwargs={"reference": self.cancelled.reference},
        )
        self.assertContains(response, f'href="{draft_planning_url}"')
        self.assertContains(response, "Choisir les animations")
        self.assertContains(response, f'href="{confirmed_planning_url}"')
        self.assertContains(response, "Modifier les animations")
        self.assertNotContains(response, f'href="{cancelled_planning_url}"')
        self.assertContains(response, f'href="{cancelled_detail_url}"')
        self.assertContains(response, "Voir la fiche")

    def test_staff_navigation_links_to_the_registration_list(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse("operations:dashboard"))

        self.assertContains(
            response,
            f'href="{reverse("operations:registration-list")}"',
        )
