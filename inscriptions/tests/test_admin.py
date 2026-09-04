from datetime import date, time
from unittest.mock import patch

from django.contrib.admin import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from catalogue.models import Animation, SchoolLevel, Session
from communication.models import EmailLog
from inscriptions.admin import RegistrationAdmin
from inscriptions.models import (
    GroupFamily,
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
    Teacher,
)


class RegistrationAdminBulkUpdateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.first_level = SchoolLevel.objects.create(
            code="ADMIN-1",
            label="Niveau initial",
            sort_order=10,
        )
        cls.second_level = SchoolLevel.objects.create(
            code="ADMIN-2",
            label="Niveau groupé",
            sort_order=20,
        )
        cls.first_family = GroupFamily.objects.create(
            name="Famille initiale",
            slug="famille-initiale",
        )
        cls.second_family = GroupFamily.objects.create(
            name="Famille groupée",
            slug="famille-groupee",
        )
        cls.institution = Institution.objects.create(
            name="Lycée test",
            institution_type=Institution.Type.HIGH_SCHOOL,
            city="Rennes",
            department="35",
        )
        cls.teacher = Teacher.objects.create(
            institution=cls.institution,
            first_name="Marie",
            last_name="Martin",
            email="marie@example.test",
            phone="0102030405",
        )
        cls.first_registration = cls._create_registration(
            group_name="Groupe un",
            student_count=20,
            token="a" * 64,
        )
        cls.second_registration = cls._create_registration(
            group_name="Groupe deux",
            student_count=25,
            token="b" * 64,
        )

    @classmethod
    def _create_registration(cls, *, group_name, student_count, token):
        return Registration.objects.create(
            institution=cls.institution,
            teacher=cls.teacher,
            group_name=group_name,
            family=cls.first_family,
            school_level=cls.first_level,
            student_count=student_count,
            chaperone_count=2,
            visit_date=date(2026, 9, 23),
            status=Registration.Status.DRAFT,
            edit_token_digest=token,
        )

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username=f"admin-{self._testMethodName}",
            email="admin@example.test",
            password="secret",
        )
        self.site = AdminSite()
        self.model_admin = RegistrationAdmin(Registration, self.site)

    def _post(self, data):
        request = RequestFactory().post("/admin/inscriptions/registration/", data=data)
        request.user = self.user
        return request

    def test_bulk_update_groups_changes_only_checked_fields_without_email(self):
        request = self._post(
            {
                "apply_bulk_update": "1",
                "apply_family": "on",
                "family": str(self.second_family.pk),
                "apply_school_level": "on",
                "school_level": str(self.second_level.pk),
                "apply_comment": "on",
                "comment": "Commentaire commun",
            }
        )
        registrations = Registration.objects.filter(
            pk__in=(self.first_registration.pk, self.second_registration.pk)
        )

        with patch.object(self.model_admin, "message_user"):
            response = self.model_admin.bulk_update_registrations(request, registrations)

        self.assertIsNone(response)
        self.first_registration.refresh_from_db()
        self.second_registration.refresh_from_db()
        self.assertEqual(self.first_registration.family, self.second_family)
        self.assertEqual(self.second_registration.family, self.second_family)
        self.assertEqual(self.first_registration.school_level, self.second_level)
        self.assertEqual(self.second_registration.school_level, self.second_level)
        self.assertEqual(self.first_registration.comment, "Commentaire commun")
        self.assertEqual(self.second_registration.comment, "Commentaire commun")
        self.assertEqual(self.first_registration.student_count, 20)
        self.assertEqual(self.second_registration.student_count, 25)
        self.assertEqual(
            RegistrationEvent.objects.filter(event_type=RegistrationEvent.Type.UPDATED).count(),
            2,
        )
        self.assertFalse(EmailLog.objects.exists())

    def test_bulk_update_rejects_day_incompatible_with_existing_reservation(self):
        animation = Animation.objects.create(
            title="Animation réservée",
            slug="animation-reservee-admin",
            short_description="Description",
            venue_category=Animation.VenueCategory.INDOOR,
            indicative_duration=60,
        )
        session = Session.objects.create(
            animation=animation,
            date=date(2026, 9, 23),
            starts_at=time(10),
            ends_at=time(11),
            location="Salle 1",
            max_capacity=30,
        )
        Reservation.objects.create(
            registration=self.first_registration,
            session=session,
            student_count=20,
            chaperone_count=2,
        )
        request = self._post(
            {
                "apply_bulk_update": "1",
                "apply_visit_date": "on",
                "visit_date": "2026-09-24",
            }
        )
        registrations = Registration.objects.filter(
            pk__in=(self.first_registration.pk, self.second_registration.pk)
        )

        response = self.model_admin.bulk_update_registrations(request, registrations)

        self.assertEqual(response.template_name, "admin/bulk_update_selected.html")
        self.assertIn("visit_date", response.context_data["form"].errors)
        self.first_registration.refresh_from_db()
        self.second_registration.refresh_from_db()
        self.assertEqual(self.first_registration.visit_date, date(2026, 9, 23))
        self.assertEqual(self.second_registration.visit_date, date(2026, 9, 23))
        self.assertFalse(
            RegistrationEvent.objects.filter(event_type=RegistrationEvent.Type.UPDATED).exists()
        )
