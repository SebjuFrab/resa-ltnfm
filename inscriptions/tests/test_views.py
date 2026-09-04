from datetime import date, datetime, time

from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from catalogue.models import Animation, Category, SchoolLevel, Session
from communication.models import EmailLog
from inscriptions.models import Institution, Registration, Reservation, Teacher
from inscriptions.services.registration import create_draft
from inscriptions.services.tokens import rotate_registration_token
from inscriptions.views import (
    COMPLETED_SESSION_KEY,
    DRAFTS_SESSION_KEY,
    MANAGED_SESSION_KEY,
)


@override_settings(
    EVENT_DATES=("2026-09-23", "2026-09-24"),
    REGISTRATION_EDIT_DEADLINE=datetime.fromisoformat("2099-09-16T23:59:00+02:00"),
)
class PublicRegistrationViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.level = SchoolLevel.objects.create(code="CM2", label="CM2", sort_order=20)
        cls.category = Category.objects.create(name="Agriculture", slug="agriculture")
        cls.animation = Animation.objects.create(
            title="Cultiver demain",
            slug="cultiver-demain",
            short_description="Découvrir une agriculture durable.",
            category=cls.category,
            indicative_duration=60,
        )
        cls.open_session = Session.objects.create(
            animation=cls.animation,
            date=date(2026, 9, 23),
            starts_at=time(9),
            ends_at=time(10),
            location="Hall 1",
            max_capacity=30,
        )
        cls.closed_session = Session.objects.create(
            animation=cls.animation,
            date=date(2026, 9, 23),
            starts_at=time(10),
            ends_at=time(11),
            location="Hall 2",
            max_capacity=30,
            status=Session.Status.CLOSED,
        )
        cls.institution = Institution.objects.create(
            name="École des Tilleuls",
            institution_type=Institution.Type.PRIMARY_SCHOOL,
            address="1 rue des Écoles",
            postal_code="35000",
            city="Rennes",
            department="35",
            administrative_email="direction@example.test",
        )
        cls.teacher = Teacher.objects.create(
            institution=cls.institution,
            first_name="Alice",
            last_name="Martin",
            email="alice.martin@example.test",
            phone="0102030405",
        )

    def setUp(self):
        cache.clear()

    def start_payload(self, *, group_name="CM2 A"):
        return {
            "existing_institution": str(self.institution.pk),
            "teacher_first_name": "Camille",
            "teacher_last_name": "Durand",
            "teacher_email": "camille.durand@example.test",
            "teacher_phone": "0601020304",
            "group_name": group_name,
            "school_level": str(self.level.pk),
            "student_count": "24",
            "chaperone_count": "3",
            "visit_date": "2026-09-23",
            "special_needs": "",
            "comment": "",
        }

    def start_draft(self, *, group_name="CM2 A"):
        response = self.client.post(
            reverse("registration-start"),
            self.start_payload(group_name=group_name),
        )
        registration = Registration.objects.get(group_name=group_name)
        return response, registration

    def create_service_draft(self, *, group_name):
        return create_draft(
            institution=self.institution,
            teacher=self.teacher,
            group_name=group_name,
            school_level=self.level,
            student_count=24,
            chaperone_count=3,
            visit_date=date(2026, 9, 23),
        )

    def test_start_creates_a_draft_for_the_existing_institution(self):
        response, registration = self.start_draft()

        self.assertRedirects(
            response,
            reverse(
                "registration-planning",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        self.assertEqual(Institution.objects.count(), 1)
        self.assertEqual(registration.institution, self.institution)
        self.assertEqual(registration.teacher.institution, self.institution)
        self.assertEqual(registration.status, Registration.Status.DRAFT)
        self.assertIsNotNone(registration.draft_expires_at)
        self.assertIn(
            str(registration.reference),
            self.client.session[DRAFTS_SESSION_KEY],
        )

    def test_planning_is_only_available_to_the_browser_that_created_the_draft(self):
        _response, registration = self.start_draft()
        planning_url = reverse(
            "registration-planning",
            kwargs={"reference": registration.reference},
        )

        owner_response = self.client.get(planning_url)
        other_browser_response = Client().get(planning_url)

        self.assertEqual(owner_response.status_code, 200)
        self.assertContains(owner_response, registration.group_name)
        self.assertEqual(other_browser_response.status_code, 404)

    def test_session_choice_then_confirmation_completes_the_registration(self):
        _response, registration = self.start_draft()
        planning_url = reverse(
            "registration-planning",
            kwargs={"reference": registration.reference},
        )

        planning_response = self.client.post(
            planning_url,
            {f"session_{self.open_session.pk}": "18"},
        )
        review_url = reverse(
            "registration-review",
            kwargs={"reference": registration.reference},
        )
        self.assertRedirects(
            planning_response,
            review_url,
            fetch_redirect_response=False,
        )
        reservation = Reservation.objects.get(
            registration=registration,
            status=Reservation.Status.ACTIVE,
        )
        self.assertEqual(reservation.session, self.open_session)
        self.assertEqual(reservation.student_count, 18)

        with self.captureOnCommitCallbacks(execute=True):
            confirmation_response = self.client.post(review_url, {"confirm": "on"})

        complete_url = reverse(
            "registration-complete",
            kwargs={"reference": registration.reference},
        )
        self.assertRedirects(
            confirmation_response,
            complete_url,
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(registration.status, Registration.Status.CONFIRMED)
        self.assertIsNone(registration.draft_expires_at)
        self.assertNotIn(
            str(registration.reference),
            self.client.session.get(DRAFTS_SESSION_KEY, []),
        )
        self.assertIn(
            str(registration.reference),
            self.client.session[COMPLETED_SESSION_KEY],
        )
        self.assertEqual(
            self.client.session[MANAGED_SESSION_KEY]["reference"],
            str(registration.reference),
        )
        self.assertEqual(
            EmailLog.objects.get(registration=registration).status,
            EmailLog.Status.SENT,
        )
        self.assertEqual(mail.outbox[0].cc, [])
        complete_response = self.client.get(complete_url)
        self.assertEqual(complete_response.status_code, 200)
        self.assertContains(complete_response, registration.group_name)

    def test_over_capacity_planning_requires_warning_then_final_confirmation(self):
        Session.objects.filter(pk=self.open_session.pk).update(max_capacity=20)
        _response, registration = self.start_draft(group_name="CM2 surcharge")
        planning_url = reverse(
            "registration-planning",
            kwargs={"reference": registration.reference},
        )
        payload = {f"session_{self.open_session.pk}": "18"}

        warning_response = self.client.post(planning_url, payload)

        self.assertEqual(warning_response.status_code, 200)
        self.assertContains(warning_response, "Attention : la jauge sera dépassée")
        self.assertContains(warning_response, 'name="confirm_over_capacity"')
        self.assertFalse(registration.reservations.exists())

        payload["confirm_over_capacity"] = "yes"
        save_response = self.client.post(planning_url, payload)
        review_url = reverse(
            "registration-review",
            kwargs={"reference": registration.reference},
        )
        self.assertRedirects(save_response, review_url, fetch_redirect_response=False)
        reservation = registration.reservations.get(status=Reservation.Status.ACTIVE)
        self.assertEqual(reservation.total_participant_count, 21)

        review_response = self.client.get(review_url)
        self.assertContains(review_response, "cette inscription dépasse la jauge")
        self.assertContains(review_response, "validation finale")

        with self.captureOnCommitCallbacks(execute=True):
            confirmation_response = self.client.post(review_url, {"confirm": "on"})

        self.assertRedirects(
            confirmation_response,
            reverse(
                "registration-complete",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(registration.status, Registration.Status.CONFIRMED)

    def test_valid_edit_link_only_grants_access_to_its_registration(self):
        first = self.create_service_draft(group_name="Groupe autorisé")
        second = self.create_service_draft(group_name="Groupe non autorisé")
        first_link = reverse(
            "registration-edit-link",
            kwargs={"reference": first.registration.reference},
        )

        landing_response = self.client.get(first_link)
        self.assertEqual(landing_response.status_code, 200)
        self.assertNotContains(landing_response, first.edit_token)
        link_response = self.client.post(first_link, {"token": first.edit_token})

        self.assertRedirects(link_response, reverse("registration-manage"))
        self.assertEqual(
            self.client.session[MANAGED_SESSION_KEY]["reference"],
            str(first.registration.reference),
        )
        manage_response = self.client.get(reverse("registration-manage"))
        self.assertContains(manage_response, first.registration.group_name)
        self.assertNotContains(manage_response, second.registration.group_name)

        mismatched_link = reverse(
            "registration-edit-link",
            kwargs={"reference": second.registration.reference},
        )
        mismatch_response = self.client.post(
            mismatched_link, {"token": first.edit_token}
        )

        self.assertEqual(mismatch_response.status_code, 404)
        self.assertEqual(
            self.client.session[MANAGED_SESSION_KEY]["reference"],
            str(first.registration.reference),
        )

    def test_invalid_edit_token_does_not_open_a_management_session(self):
        access = self.create_service_draft(group_name="Groupe protégé")
        invalid_link = reverse(
            "registration-edit-link",
            kwargs={"reference": access.registration.reference},
        )

        response = self.client.post(invalid_link, {"token": "jeton-incorrect"})

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Lien de modification invalide", status_code=404)
        self.assertNotIn(MANAGED_SESSION_KEY, self.client.session)

    def test_token_rotation_invalidates_an_already_open_management_session(self):
        access = self.create_service_draft(group_name="Groupe avec rotation")
        link = reverse(
            "registration-edit-link",
            kwargs={"reference": access.registration.reference},
        )
        self.client.post(link, {"token": access.edit_token})
        self.assertEqual(self.client.get(reverse("registration-manage")).status_code, 200)

        rotate_registration_token(access.registration)

        self.assertEqual(self.client.get(reverse("registration-manage")).status_code, 403)

    def test_editing_shared_teacher_does_not_change_another_registration(self):
        first = self.create_service_draft(group_name="Premier groupe")
        second = self.create_service_draft(group_name="Second groupe")
        link = reverse(
            "registration-edit-link",
            kwargs={"reference": first.registration.reference},
        )
        self.client.post(link, {"token": first.edit_token})

        response = self.client.post(
            reverse("registration-manage-details"),
            {
                "teacher_first_name": "Nouvelle",
                "teacher_last_name": "Adresse",
                "teacher_email": "nouvelle@example.test",
                "teacher_phone": "0199999999",
                "group_name": first.registration.group_name,
                "school_level": self.level.pk,
                "student_count": first.registration.student_count,
                "chaperone_count": first.registration.chaperone_count,
                "special_needs": "",
                "comment": "",
            },
        )

        self.assertRedirects(response, reverse("registration-manage"))
        first.registration.refresh_from_db()
        second.registration.refresh_from_db()
        self.assertNotEqual(first.registration.teacher_id, second.registration.teacher_id)
        self.assertEqual(second.registration.teacher.email, "alice.martin@example.test")
        audit = first.registration.events.filter(
            changes__has_key="teacher_fields"
        ).get()
        self.assertEqual(
            audit.changes["teacher_fields"],
            ["email", "first_name", "last_name", "phone"],
        )

    def test_closed_session_cannot_be_reserved_or_confirmed(self):
        _response, registration = self.start_draft(group_name="CM2 fermeture")
        planning_url = reverse(
            "registration-planning",
            kwargs={"reference": registration.reference},
        )

        response = self.client.post(
            planning_url,
            {f"session_{self.closed_session.pk}": "15"},
        )

        review_url = reverse(
            "registration-review",
            kwargs={"reference": registration.reference},
        )
        self.assertRedirects(response, review_url, fetch_redirect_response=False)
        self.assertFalse(registration.reservations.exists())

        confirmation_response = self.client.post(review_url, {"confirm": "on"})
        registration.refresh_from_db()
        self.assertEqual(confirmation_response.status_code, 200)
        self.assertContains(
            confirmation_response,
            "Au moins une séance doit être choisie avant la confirmation.",
        )
        self.assertEqual(registration.status, Registration.Status.DRAFT)
