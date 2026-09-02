from datetime import date, time, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalogue.models import Animation, Category, SchoolLevel, Session, Theme
from communication.models import EmailLog, MailingCampaign, MailingDelivery
from inscriptions.models import (
    GroupFamily,
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
)
from operations.forms import (
    AnimationFilterForm,
    StaffPlanningForm,
    StaffRegistrationForm,
)


class InternalRegistrationWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_superuser(
            username="frab", email="frab@example.test", password="secret"
        )
        cls.family = GroupFamily.objects.create(
            name="Enseignement agricole", slug="enseignement-agricole"
        )
        cls.level = SchoolLevel.objects.create(code="LYCEE", label="Lycée")
        cls.category = Category.objects.create(name="Sol", slug="sol")
        cls.animation = Animation.objects.create(
            title="Découvrir le sol vivant",
            slug="decouvrir-sol-vivant",
            short_description="Une animation de terrain.",
            category=cls.category,
            venue_category=Animation.VenueCategory.OUTDOOR,
            indicative_duration=60,
        )
        cls.theme = Theme.objects.get(slug="sol")
        cls.animation.themes.add(cls.theme)
        cls.session = Session.objects.create(
            animation=cls.animation,
            date=date(2026, 9, 23),
            starts_at=time(10),
            ends_at=time(11),
            location="Pôle sols",
            max_capacity=40,
            organizer="Alice Responsable",
            organizer_email="animation@example.test",
        )

    def setUp(self):
        self.client.force_login(self.staff)

    def registration_payload(self, **overrides):
        payload = {
            "existing_institution": "",
            "institution_type": Institution.Type.AGRICULTURAL,
            "institution_name": "Lycée de la Vallée",
            "institution_city": "Rennes",
            "institution_department": "35",
            "teacher_last_name": "Martin",
            "teacher_first_name": "Camille",
            "teacher_email": "camille@example.test",
            "teacher_phone": "0601020304",
            "group_code": "truffe-doree",
            "family": str(self.family.pk),
            "school_level": str(self.level.pk),
            "visit_date": "2026-09-23",
            "student_count": "24",
            "chaperone_count": "2",
            "level_comment": "Seconde et première",
            "comment": "Arrivée en car.",
        }
        payload.update(overrides)
        return payload

    def planning_payload(self, session, *, students=24, chaperones=2):
        return {
            StaffPlanningForm.student_field_name(session.pk): str(students),
            StaffPlanningForm.chaperone_field_name(session.pk): str(chaperones),
        }

    def create_and_confirm_registration(self):
        response = self.client.post(
            reverse("operations:registration-create"), self.registration_payload()
        )
        registration = Registration.objects.get(group_code="truffe-doree")
        self.assertRedirects(
            response,
            reverse(
                "operations:registration-planning",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )

        response = self.client.post(
            reverse(
                "operations:registration-planning",
                kwargs={"reference": registration.reference},
            ),
            self.planning_payload(self.session),
        )
        self.assertRedirects(
            response,
            reverse(
                "operations:registration-review",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse(
                    "operations:registration-review",
                    kwargs={"reference": registration.reference},
                ),
                {"confirm": "on"},
            )
        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        return registration

    def test_anonymous_home_is_sent_to_staff_login(self):
        self.client.logout()
        response = self.client.get(reverse("home"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("/admin/login/", response.redirect_chain[-1][0])

    def test_staff_creates_and_confirms_a_full_group(self):
        registration = self.create_and_confirm_registration()

        self.assertEqual(registration.status, Registration.Status.CONFIRMED)
        self.assertEqual(registration.group_code, "truffe-doree")
        self.assertEqual(registration.family, self.family)
        self.assertEqual(
            registration.institution.institution_type,
            Institution.Type.AGRICULTURAL,
        )
        self.assertEqual(registration.total_participant_count, 26)
        reservation = Reservation.objects.get(registration=registration)
        self.assertEqual(reservation.student_count, 24)
        self.assertEqual(reservation.chaperone_count, 2)
        self.assertEqual(reservation.total_participant_count, 26)
        self.session.refresh_from_db()
        self.assertEqual(self.session.remaining_capacity, 14)
        created_event = registration.events.get(event_type=RegistrationEvent.Type.CREATED)
        self.assertEqual(created_event.actor_kind, RegistrationEvent.ActorKind.STAFF)
        self.assertEqual(created_event.actor_user, self.staff)
        self.assertEqual(EmailLog.objects.get(registration=registration).kind, "CONFIRMATION")
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("lien de modification", mail.outbox[0].body.lower())

    def test_staff_can_save_a_persistent_draft_that_holds_places_without_email(self):
        self.client.post(
            reverse("operations:registration-create"),
            self.registration_payload(),
        )
        registration = Registration.objects.get(group_code="truffe-doree")
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        payload = self.planning_payload(self.session)
        payload["action"] = "save_draft"

        response = self.client.post(planning_url, payload)

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(registration.status, Registration.Status.DRAFT)
        self.assertIsNone(registration.draft_expires_at)
        self.assertEqual(
            registration.reservations.get(status=Reservation.Status.ACTIVE).total_participant_count,
            26,
        )
        future_session = Session.objects.with_capacities(
            at=timezone.now() + timedelta(days=365)
        ).get(pk=self.session.pk)
        self.assertEqual(future_session.remaining_capacity, 14)
        self.assertEqual(EmailLog.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_can_leave_review_as_a_draft_without_email(self):
        self.client.post(
            reverse("operations:registration-create"),
            self.registration_payload(),
        )
        registration = Registration.objects.get(group_code="truffe-doree")
        self.client.post(
            reverse(
                "operations:registration-planning",
                kwargs={"reference": registration.reference},
            ),
            self.planning_payload(self.session),
        )
        review_url = reverse(
            "operations:registration-review",
            kwargs={"reference": registration.reference},
        )

        response = self.client.post(review_url, {"action": "save_draft"})

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(registration.status, Registration.Status.DRAFT)
        self.assertTrue(registration.reservations.filter(status="ACTIVE").exists())
        self.assertEqual(EmailLog.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_can_split_students_and_chaperones_between_overlapping_sessions(self):
        second_session = Session.objects.create(
            animation=self.animation,
            date=date(2026, 9, 23),
            starts_at=time(10, 15),
            ends_at=time(10, 45),
            location="Pôle sols bis",
            max_capacity=40,
        )
        response = self.client.post(
            reverse("operations:registration-create"), self.registration_payload()
        )
        registration = Registration.objects.get(group_code="truffe-doree")
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )

        planning_page = self.client.get(planning_url)
        self.assertContains(
            planning_page,
            StaffPlanningForm.student_field_name(self.session.pk),
        )
        self.assertContains(planning_page, "Tout le groupe (26)")
        self.assertContains(planning_page, "Saisissez une partie du groupe")
        self.assertContains(planning_page, 'data-layout="compact-grid"')
        self.assertContains(planning_page, "planning-session-card", count=2)
        self.assertNotContains(planning_page, 'id="time-')
        self.assertRedirects(response, planning_url, fetch_redirect_response=False)

        payload = {}
        payload.update(self.planning_payload(self.session, students=12, chaperones=1))
        payload.update(
            self.planning_payload(second_session, students=12, chaperones=1)
        )
        response = self.client.post(planning_url, payload)

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-review",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        allocations = list(
            registration.reservations.filter(status=Reservation.Status.ACTIVE)
            .order_by("session__starts_at")
            .values_list("student_count", "chaperone_count")
        )
        self.assertEqual(allocations, [(12, 1), (12, 1)])
        self.assertEqual(self.session.remaining_capacity, 27)
        self.assertEqual(second_session.remaining_capacity, 27)

    def test_staff_can_confirm_only_part_of_the_group_for_a_session(self):
        self.client.post(
            reverse("operations:registration-create"), self.registration_payload()
        )
        registration = Registration.objects.get(group_code="truffe-doree")
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        review_url = reverse(
            "operations:registration-review",
            kwargs={"reference": registration.reference},
        )

        response = self.client.post(
            planning_url,
            self.planning_payload(self.session, students=10, chaperones=1),
        )
        self.assertRedirects(response, review_url, fetch_redirect_response=False)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(review_url, {"confirm": "on"})

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        reservation = registration.reservations.get(status=Reservation.Status.ACTIVE)
        self.assertEqual(registration.status, Registration.Status.CONFIRMED)
        self.assertEqual(
            (reservation.student_count, reservation.chaperone_count), (10, 1)
        )
        self.assertEqual(self.session.remaining_capacity, 29)

    def test_partial_allocation_requires_an_over_capacity_confirmation(self):
        self.session.max_capacity = 10
        self.session.save(update_fields=("max_capacity", "updated_at"))
        self.client.post(
            reverse("operations:registration-create"), self.registration_payload()
        )
        registration = Registration.objects.get(group_code="truffe-doree")
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )

        response = self.client.post(
            planning_url,
            self.planning_payload(self.session, students=9, chaperones=2),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Attention : la jauge sera dépassée")
        self.assertContains(response, 'name="confirm_over_capacity"')
        self.assertFalse(registration.reservations.exists())

        payload = self.planning_payload(self.session, students=9, chaperones=2)
        payload["confirm_over_capacity"] = "yes"
        response = self.client.post(planning_url, payload)

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-review",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        reservation = registration.reservations.get(status=Reservation.Status.ACTIVE)
        self.assertEqual(reservation.total_participant_count, 11)

    def test_filtered_planning_preserves_hidden_partial_allocation(self):
        self.client.post(
            reverse("operations:registration-create"), self.registration_payload()
        )
        registration = Registration.objects.get(group_code="truffe-doree")
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        self.client.post(
            planning_url,
            self.planning_payload(self.session, students=12, chaperones=1),
        )
        other_animation = Animation.objects.create(
            title="Comprendre le climat",
            slug="comprendre-climat",
            short_description="Une animation sur le climat.",
            category=self.category,
            indicative_duration=45,
        )
        other_session = Session.objects.create(
            animation=other_animation,
            date=date(2026, 9, 23),
            starts_at=time(12),
            ends_at=time(12, 45),
            location="Pôle climat",
            max_capacity=40,
        )

        self.client.post(
            f"{planning_url}?q=climat",
            self.planning_payload(other_session, students=6, chaperones=1),
        )

        allocations = {
            reservation.session_id: (
                reservation.student_count,
                reservation.chaperone_count,
            )
            for reservation in registration.reservations.filter(
                status=Reservation.Status.ACTIVE
            )
        }
        self.assertEqual(allocations[self.session.pk], (12, 1))
        self.assertEqual(allocations[other_session.pk], (6, 1))

    def test_filtered_planning_accepts_preserved_unsaved_allocations(self):
        self.client.post(
            reverse("operations:registration-create"), self.registration_payload()
        )
        registration = Registration.objects.get(group_code="truffe-doree")
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        other_animation = Animation.objects.create(
            title="Comprendre le climat",
            slug="comprendre-climat-brouillon",
            short_description="Une animation sur le climat.",
            category=self.category,
            indicative_duration=45,
        )
        other_session = Session.objects.create(
            animation=other_animation,
            date=date(2026, 9, 23),
            starts_at=time(12),
            ends_at=time(12, 45),
            location="Pôle climat",
            max_capacity=40,
        )
        payload = {}
        payload.update(
            self.planning_payload(self.session, students=12, chaperones=1)
        )
        payload.update(
            self.planning_payload(other_session, students=6, chaperones=1)
        )

        response = self.client.post(f"{planning_url}?q=climat", payload)

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-review",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        allocations = {
            reservation.session_id: (
                reservation.student_count,
                reservation.chaperone_count,
            )
            for reservation in registration.reservations.filter(
                status=Reservation.Status.ACTIVE
            )
        }
        self.assertEqual(allocations[self.session.pk], (12, 1))
        self.assertEqual(allocations[other_session.pk], (6, 1))

    def test_pending_effectif_change_survives_planning_filters(self):
        registration = self.create_and_confirm_registration()
        mail.outbox.clear()
        update_payload = self.registration_payload(
            existing_institution=str(registration.institution_id),
            institution_name="",
            institution_city="",
            institution_department="",
            student_count="20",
            chaperone_count="3",
        )
        self.client.post(
            reverse(
                "operations:registration-update",
                kwargs={"reference": registration.reference},
            ),
            update_payload,
        )
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )

        response = self.client.get(
            planning_url, {"reschedule": "1", "q": "sol"}
        )

        self.assertTrue(response.context["is_rescheduling"])
        self.assertEqual(response.context["registration"].student_count, 20)
        self.assertContains(response, 'name="reschedule" value="1"')
        self.assertContains(response, f'href="{planning_url}?reschedule=1"')

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"{planning_url}?reschedule=1&q=sol",
                self.planning_payload(self.session, students=10, chaperones=1),
            )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(
            (registration.student_count, registration.chaperone_count), (20, 3)
        )
        reservation = registration.reservations.get(status=Reservation.Status.ACTIVE)
        self.assertEqual(
            (reservation.student_count, reservation.chaperone_count), (10, 1)
        )

    def test_unavailable_reserved_animation_stays_visible_until_removed(self):
        registration = self.create_and_confirm_registration()
        self.animation.is_active = False
        self.animation.save(update_fields=("is_active", "updated_at"))
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )

        response = self.client.get(planning_url, {"q": "aucun-resultat"})

        self.assertContains(response, self.animation.title)
        self.assertContains(response, "Cette animation n’est plus disponible")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"{planning_url}?q=aucun-resultat",
                self.planning_payload(self.session, students=0, chaperones=0),
            )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        self.assertFalse(
            registration.reservations.filter(status=Reservation.Status.ACTIVE).exists()
        )
        self.assertEqual(
            registration.reservations.filter(status=Reservation.Status.CANCELLED).count(),
            1,
        )

    def test_effectif_change_can_remove_the_last_unavailable_reservation(self):
        registration = self.create_and_confirm_registration()
        self.animation.is_active = False
        self.animation.save(update_fields=("is_active", "updated_at"))
        update_payload = self.registration_payload(
            existing_institution=str(registration.institution_id),
            institution_name="",
            institution_city="",
            institution_department="",
            student_count="20",
            chaperone_count="3",
        )
        self.client.post(
            reverse(
                "operations:registration-update",
                kwargs={"reference": registration.reference},
            ),
            update_payload,
        )
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"{planning_url}?reschedule=1",
                self.planning_payload(self.session, students=0, chaperones=0),
            )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(
            (registration.student_count, registration.chaperone_count), (20, 3)
        )
        self.assertFalse(
            registration.reservations.filter(status=Reservation.Status.ACTIVE).exists()
        )

    def test_expired_draft_does_not_recover_capacity_it_no_longer_holds(self):
        self.client.post(
            reverse("operations:registration-create"), self.registration_payload()
        )
        registration = Registration.objects.get(group_code="truffe-doree")
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        self.client.post(
            planning_url,
            self.planning_payload(self.session, students=9, chaperones=1),
        )
        Registration.objects.filter(pk=registration.pk).update(
            draft_expires_at=timezone.now() - timedelta(minutes=1)
        )
        Session.objects.filter(pk=self.session.pk).update(max_capacity=10)

        response = self.client.get(planning_url)

        planning_form = response.context["planning_form"]
        student_field = planning_form.fields[
            StaffPlanningForm.student_field_name(self.session.pk)
        ]
        self.assertEqual(student_field.max_value, 24)
        self.assertTrue(response.context["session_rows"][0][3])

    def test_contact_update_preserves_partial_allocations(self):
        registration = self.create_and_confirm_registration()
        reservation = registration.reservations.get(status=Reservation.Status.ACTIVE)
        reservation.student_count = 12
        reservation.chaperone_count = 1
        reservation.save(
            update_fields=("student_count", "chaperone_count", "updated_at")
        )
        mail.outbox.clear()
        payload = self.registration_payload(
            existing_institution=str(registration.institution_id),
            institution_name="",
            institution_city="",
            institution_department="",
            comment="Contact vérifié.",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse(
                    "operations:registration-update",
                    kwargs={"reference": registration.reference},
                ),
                payload,
            )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        reservation.refresh_from_db()
        self.assertEqual(
            (reservation.student_count, reservation.chaperone_count), (12, 1)
        )

    def test_department_choices_cover_brittany_and_neighbouring_departments(self):
        field = StaffRegistrationForm().fields["institution_department"]

        for department in ("22", "29", "35", "56", "44", "49", "50", "53"):
            with self.subTest(department=department):
                self.assertTrue(field.valid_value(department))
        self.assertFalse(field.valid_value("75"))

    def test_inactive_school_level_is_hidden_but_retained_when_editing(self):
        inactive_level = SchoolLevel.objects.create(
            code="ARCHIVE",
            label="Niveau archivé",
            is_active=False,
        )

        self.assertNotIn(
            inactive_level,
            StaffRegistrationForm().fields["school_level"].queryset,
        )

        registration = Registration(
            institution=Institution(),
            school_level=inactive_level,
        )
        edit_form = StaffRegistrationForm(data={}, registration=registration)
        self.assertIn(inactive_level, edit_form.fields["school_level"].queryset)

    def test_staff_can_update_group_details_without_notifying_teacher(self):
        registration = self.create_and_confirm_registration()
        mail.outbox.clear()
        payload = self.registration_payload(
            existing_institution=str(registration.institution_id),
            institution_name="",
            institution_city="",
            institution_department="",
            comment="Note interne corrigée sans envoi.",
        )
        payload["action"] = "save_without_notification"

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse(
                    "operations:registration-update",
                    kwargs={"reference": registration.reference},
                ),
                payload,
            )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(registration.comment, "Note interne corrigée sans envoi.")
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_can_update_animations_without_notifying_teacher(self):
        registration = self.create_and_confirm_registration()
        mail.outbox.clear()
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        payload = self.planning_payload(self.session, students=20, chaperones=2)
        payload["action"] = "save_without_notification"

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(planning_url, payload)

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        reservation = registration.reservations.get(status=Reservation.Status.ACTIVE)
        self.assertEqual((reservation.student_count, reservation.chaperone_count), (20, 2))
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_effectif_update_keeps_the_without_notification_choice(self):
        registration = self.create_and_confirm_registration()
        mail.outbox.clear()
        payload = self.registration_payload(
            existing_institution=str(registration.institution_id),
            institution_name="",
            institution_city="",
            institution_department="",
            student_count="20",
            chaperone_count="3",
        )
        payload["action"] = "save_without_notification"
        self.client.post(
            reverse(
                "operations:registration-update",
                kwargs={"reference": registration.reference},
            ),
            payload,
        )
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"{planning_url}?reschedule=1",
                self.planning_payload(self.session, students=18, chaperones=2),
            )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual((registration.student_count, registration.chaperone_count), (20, 3))
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_update_reallocates_reservations_and_notifies_teacher(self):
        registration = self.create_and_confirm_registration()
        mail.outbox.clear()

        payload = self.registration_payload(
            existing_institution=str(registration.institution_id),
            institution_name="",
            institution_city="",
            institution_department="",
            student_count="20",
            chaperone_count="3",
            comment="Effectif corrigé pendant un second appel.",
        )
        response = self.client.post(
            reverse(
                "operations:registration-update",
                kwargs={"reference": registration.reference},
            ),
            payload,
        )

        self.assertRedirects(
            response,
            (
                reverse(
                    "operations:registration-planning",
                    kwargs={"reference": registration.reference},
                )
                + "?reschedule=1"
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(registration.total_participant_count, 26)
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 0)
        self.assertEqual(len(mail.outbox), 0)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                (
                    reverse(
                        "operations:registration-planning",
                        kwargs={"reference": registration.reference},
                    )
                    + "?reschedule=1"
                ),
                self.planning_payload(self.session, students=15, chaperones=2),
            )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        reservation = registration.reservations.get(status=Reservation.Status.ACTIVE)
        self.assertEqual(registration.total_participant_count, 23)
        self.assertEqual(reservation.student_count, 15)
        self.assertEqual(reservation.chaperone_count, 2)
        self.assertEqual(reservation.total_participant_count, 17)
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_day_filter_previews_then_atomically_replaces_the_program(self):
        registration = self.create_and_confirm_registration()
        previous_reservation = registration.reservations.get(
            status=Reservation.Status.ACTIVE
        )
        next_day_session = Session.objects.create(
            animation=self.animation,
            date=date(2026, 9, 24),
            starts_at=time(14),
            ends_at=time(15),
            location="Salle du 24 septembre",
            max_capacity=40,
        )
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        mail.outbox.clear()

        response = self.client.get(planning_url, {"date": "2026-09-24"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["planning_date"], date(2026, 9, 24))
        self.assertContains(response, "Salle du 24 septembre")
        self.assertNotContains(response, self.session.location)
        self.assertContains(
            response,
            "Le jour du groupe et ses réservations ne seront remplacés qu’au moment",
        )
        self.assertEqual(
            response.context["planning_post_url"],
            f"{planning_url}?date=2026-09-24",
        )
        registration.refresh_from_db()
        previous_reservation.refresh_from_db()
        self.assertEqual(registration.visit_date, date(2026, 9, 23))
        self.assertEqual(previous_reservation.status, Reservation.Status.ACTIVE)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"{planning_url}?date=2026-09-24",
                self.planning_payload(next_day_session, students=12, chaperones=1),
            )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        previous_reservation.refresh_from_db()
        active_reservation = registration.reservations.get(
            status=Reservation.Status.ACTIVE
        )
        self.assertEqual(registration.visit_date, date(2026, 9, 24))
        self.assertEqual(active_reservation.session, next_day_session)
        self.assertEqual(
            (active_reservation.student_count, active_reservation.chaperone_count),
            (12, 1),
        )
        self.assertEqual(previous_reservation.status, Reservation.Status.CANCELLED)
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_day_filter_requires_a_new_program_and_ignores_an_old_day_injection(self):
        registration = self.create_and_confirm_registration()
        previous_reservation = registration.reservations.get(
            status=Reservation.Status.ACTIVE
        )
        next_day_session = Session.objects.create(
            animation=self.animation,
            date=date(2026, 9, 24),
            starts_at=time(14),
            ends_at=time(15),
            location="Salle du lendemain",
            max_capacity=40,
        )
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        mail.outbox.clear()

        response = self.client.post(
            f"{planning_url}?date=2026-09-24",
            self.planning_payload(self.session, students=10, chaperones=1),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Sélectionnez au moins une animation avant d&#x27;enregistrer les modifications.",
        )
        registration.refresh_from_db()
        previous_reservation.refresh_from_db()
        self.assertEqual(registration.visit_date, date(2026, 9, 23))
        self.assertEqual(previous_reservation.status, Reservation.Status.ACTIVE)
        self.assertFalse(
            registration.reservations.filter(
                session=next_day_session,
                status=Reservation.Status.ACTIVE,
            ).exists()
        )
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_invalid_day_filter_cannot_change_the_group_or_its_program(self):
        registration = self.create_and_confirm_registration()
        previous_reservation = registration.reservations.get(
            status=Reservation.Status.ACTIVE
        )
        next_day_session = Session.objects.create(
            animation=self.animation,
            date=date(2026, 9, 24),
            starts_at=time(14),
            ends_at=time(15),
            location="Salle du lendemain",
            max_capacity=40,
        )
        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        mail.outbox.clear()

        response = self.client.post(
            f"{planning_url}?date=2026-09-25",
            self.planning_payload(next_day_session, students=10, chaperones=1),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Le jour ou l’un des filtres est invalide.",
        )
        registration.refresh_from_db()
        previous_reservation.refresh_from_db()
        self.assertEqual(registration.visit_date, date(2026, 9, 23))
        self.assertEqual(previous_reservation.status, Reservation.Status.ACTIVE)
        self.assertFalse(
            registration.reservations.filter(
                session=next_day_session,
                status=Reservation.Status.ACTIVE,
            ).exists()
        )
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_day_change_notifies_only_after_the_new_program_is_selected(self):
        registration = self.create_and_confirm_registration()
        next_day_session = Session.objects.create(
            animation=self.animation,
            date=date(2026, 9, 24),
            starts_at=time(10),
            ends_at=time(11),
            location="Pôle sols",
            max_capacity=40,
            organizer="Alice Responsable",
            organizer_email="animation@example.test",
        )
        mail.outbox.clear()
        payload = self.registration_payload(
            existing_institution=str(registration.institution_id),
            institution_name="",
            institution_city="",
            institution_department="",
            visit_date="2026-09-24",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse(
                    "operations:registration-update",
                    kwargs={"reference": registration.reference},
                ),
                payload,
            )

        self.assertRedirects(
            response,
            (
                reverse(
                    "operations:registration-planning",
                    kwargs={"reference": registration.reference},
                )
                + "?reschedule=1"
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(registration.visit_date, date(2026, 9, 23))
        self.assertEqual(
            registration.reservations.filter(status=Reservation.Status.ACTIVE).count(),
            1,
        )
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 0)
        self.assertEqual(len(mail.outbox), 0)

        planning_url = reverse(
            "operations:registration-planning",
            kwargs={"reference": registration.reference},
        )
        planning_page = self.client.get(f"{planning_url}?reschedule=1")
        self.assertEqual(planning_page.context["planning_date"], date(2026, 9, 24))
        self.assertEqual(
            planning_page.context["planning_post_url"],
            f"{planning_url}?reschedule=1&date=2026-09-24",
        )
        self.assertContains(planning_page, 'name="date"')

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"{planning_url}?reschedule=1",
                self.planning_payload(
                    next_day_session, students=18, chaperones=2
                ),
            )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(registration.visit_date, date(2026, 9, 24))
        self.assertEqual(
            registration.reservations.get(status=Reservation.Status.ACTIVE).session,
            next_day_session,
        )
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_abandoned_day_change_keeps_the_confirmed_program(self):
        registration = self.create_and_confirm_registration()
        payload = self.registration_payload(
            existing_institution=str(registration.institution_id),
            institution_name="",
            institution_city="",
            institution_department="",
            visit_date="2026-09-24",
        )
        self.client.post(
            reverse(
                "operations:registration-update",
                kwargs={"reference": registration.reference},
            ),
            payload,
        )

        response = self.client.post(
            (
                reverse(
                    "operations:registration-planning",
                    kwargs={"reference": registration.reference},
                )
                + "?reschedule=1"
            ),
            {"action": "cancel-reschedule"},
        )

        self.assertRedirects(
            response,
            reverse(
                "operations:registration-detail",
                kwargs={"reference": registration.reference},
            ),
            fetch_redirect_response=False,
        )
        registration.refresh_from_db()
        self.assertEqual(registration.visit_date, date(2026, 9, 23))
        self.assertEqual(
            registration.reservations.filter(status=Reservation.Status.ACTIVE).count(),
            1,
        )

    def test_repeated_cancellation_does_not_send_a_second_email(self):
        registration = self.create_and_confirm_registration()
        mail.outbox.clear()
        cancel_url = reverse(
            "operations:registration-cancel",
            kwargs={"reference": registration.reference},
        )

        with self.captureOnCommitCallbacks(execute=True):
            first_response = self.client.post(cancel_url, {"confirm": "on"})
        with self.captureOnCommitCallbacks(execute=True):
            second_response = self.client.post(cancel_url, {"confirm": "on"})

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(EmailLog.objects.filter(kind="CANCELLATION").count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_animation_filters_show_responsible_and_total_capacity(self):
        response = self.client.get(
            reverse("operations:animation-list"),
            {"q": "Alice", "date": "2026-09-23", "available_only": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.animation.title)
        self.assertContains(response, "animation@example.test")
        self.assertEqual(len(response.context["sessions"]), 1)

    def test_animation_filters_include_category_and_theme_and_auto_submit(self):
        list_filter_form = AnimationFilterForm()
        self.assertIn("category", list_filter_form.fields)
        self.assertIn("theme", list_filter_form.fields)
        self.assertNotIn("level", list_filter_form.fields)
        Theme.objects.filter(slug="eau").update(is_active=False)
        self.assertFalse(
            list_filter_form.fields["theme"].queryset.filter(slug="eau").exists()
        )

        indoor_animation = Animation.objects.create(
            title="Conférence climat",
            slug="conference-climat",
            short_description="Une conférence en salle.",
            venue_category=Animation.VenueCategory.INDOOR,
            indicative_duration=60,
        )
        indoor_animation.themes.add(Theme.objects.get(slug="climat"))
        Session.objects.create(
            animation=indoor_animation,
            date=date(2026, 9, 23),
            starts_at=time(14),
            ends_at=time(15),
            location="Salle A",
            max_capacity=40,
        )

        response = self.client.get(
            reverse("operations:animation-list"),
            {
                "category": Animation.VenueCategory.OUTDOOR,
                "theme": self.theme.pk,
            },
        )

        self.assertContains(response, "data-auto-submit-filters")
        self.assertContains(response, "Les résultats se mettent à jour automatiquement")
        self.assertContains(response, "auto-submit-filters")
        self.assertNotContains(response, "Appliquer les filtres")
        self.assertContains(response, 'name="category"')
        self.assertContains(response, 'name="theme"')
        self.assertNotContains(response, 'name="level"')
        self.assertEqual(response.context["sessions"], [self.session])
        self.assertContains(response, "Catégorie")
        self.assertContains(response, "Thématiques")
        self.assertNotContains(response, indoor_animation.title)

    def test_registration_planning_matches_animation_auto_filters(self):
        response = self.client.post(
            reverse("operations:registration-create"), self.registration_payload()
        )

        planning_response = self.client.get(response.url)

        self.assertContains(planning_response, "data-auto-submit-filters")
        self.assertContains(
            planning_response, "Les résultats se mettent à jour automatiquement"
        )
        self.assertContains(planning_response, "auto-submit-filters")
        self.assertContains(planning_response, "data-filter-draft")
        self.assertContains(planning_response, "data-unfiltered-action")
        self.assertContains(planning_response, "data-clear-filters")
        self.assertNotContains(planning_response, "Appliquer les filtres")
        self.assertContains(planning_response, 'name="date"')
        self.assertContains(planning_response, 'name="category"')
        self.assertContains(planning_response, 'name="theme"')
        self.assertNotContains(planning_response, 'name="level"')
        self.assertEqual(
            set(planning_response.context["filter_form"].fields),
            {
                "q",
                "date",
                "category",
                "theme",
                "starts_after",
                "ends_before",
                "status",
                "available_only",
            },
        )

    def test_auto_filters_restore_scroll_focus_and_planning_draft(self):
        script = (
            settings.BASE_DIR / "static" / "js" / "auto-submit-filters.js"
        ).read_text(encoding="utf-8")

        for expected_code in (
            "window.scrollY",
            "window.scrollTo(0, scrollTop)",
            'window.history.scrollRestoration = "manual"',
            'root.style.scrollBehavior = "auto"',
            "preventScroll: true",
            'document.querySelector("form[data-filter-draft]")',
            "const readStoredDraft",
            "planningForm.dataset.unfilteredAction",
        ):
            with self.subTest(expected_code=expected_code):
                self.assertIn(expected_code, script)

    def test_registration_planning_uses_three_compact_columns_on_desktop(self):
        stylesheet = (settings.BASE_DIR / "static" / "css" / "app.css").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "grid-template-columns: repeat(3, minmax(0, 1fr));",
            stylesheet,
        )
        self.assertIn("@media (max-width: 62rem)", stylesheet)
        self.assertIn("@media (max-width: 44rem)", stylesheet)

    def test_final_mailing_sends_individual_teacher_and_organizer_messages(self):
        self.create_and_confirm_registration()
        mail.outbox.clear()

        response = self.client.get(reverse("operations:mailing-create"))
        self.assertEqual(response.context["preview"].teacher_count, 1)
        self.assertEqual(response.context["preview"].organizer_count, 1)
        for template_variable in (
            "{{ prenom }}",
            "{{ nom }}",
            "{{ programme }}",
            "{{ nombre_inscrits }}",
        ):
            self.assertContains(response, template_variable)
        self.assertContains(response, "Message aux responsables de groupe")
        self.assertContains(response, "Message aux responsables d’animation")
        self.assertContains(response, "Envoyer aux responsables de groupe")
        self.assertContains(response, "Envoyer aux responsables d’animation")
        self.assertContains(response, "Envoyer aux deux publics")

        response = self.client.post(
            reverse("operations:mailing-create"),
            {
                "visit_date": "2026-09-23",
                "family": str(self.family.pk),
                "subject": "Préparer votre venue",
                "body_html": "<p><strong>Accueil à 9 h.</strong></p><script>alert(1)</script>",
                "organizer_subject": "Préparer l’accueil des groupes",
                "organizer_body_html": "<p>Consignes réservées aux animateurs.</p>",
                "idempotency_key": "test-final-mailing",
                "action": "send",
            },
        )

        campaign = MailingCampaign.objects.get(idempotency_key="test-final-mailing")
        self.assertRedirects(
            response,
            reverse("operations:mailing-detail", kwargs={"campaign_id": campaign.pk}),
            fetch_redirect_response=False,
        )
        self.assertNotIn("script", campaign.body_html)
        self.assertEqual(campaign.organizer_subject, "Préparer l’accueil des groupes")
        self.assertEqual(campaign.deliveries.count(), 2)
        self.assertFalse(
            campaign.deliveries.exclude(status=MailingDelivery.Status.SENT).exists()
        )
        self.assertCountEqual(
            [message.to[0] for message in mail.outbox],
            ["camille@example.test", "animation@example.test"],
        )
        group_message = next(
            message for message in mail.outbox if message.to == ["camille@example.test"]
        )
        organizer_message = next(
            message
            for message in mail.outbox
            if message.to == ["animation@example.test"]
        )
        self.assertEqual(group_message.subject, "Préparer votre venue")
        self.assertEqual(organizer_message.subject, "Préparer l’accueil des groupes")
        self.assertIn("Accueil à 9 h.", group_message.body)
        self.assertNotIn("Consignes réservées aux animateurs", group_message.body)
        self.assertIn("Consignes réservées aux animateurs", organizer_message.body)
        self.assertNotIn("Accueil à 9 h.", organizer_message.body)

        detail_response = self.client.get(
            reverse("operations:mailing-detail", kwargs={"campaign_id": campaign.pk})
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Préparer votre venue")
        self.assertContains(detail_response, "Préparer l’accueil des groupes")
        self.assertContains(detail_response, "Responsables de groupe")
        self.assertContains(detail_response, "Responsables d’animation")

    def test_final_mailing_can_send_each_audience_separately(self):
        self.create_and_confirm_registration()
        mail.outbox.clear()

        group_response = self.client.post(
            reverse("operations:mailing-create"),
            {
                "visit_date": "2026-09-23",
                "family": str(self.family.pk),
                "subject": "Message pour le groupe",
                "body_html": "<p>Uniquement pour le responsable du groupe.</p>",
                "idempotency_key": "mailing-group-only",
                "action": "send_groups",
            },
        )

        group_campaign = MailingCampaign.objects.get(
            idempotency_key="mailing-group-only"
        )
        self.assertRedirects(
            group_response,
            reverse(
                "operations:mailing-detail",
                kwargs={"campaign_id": group_campaign.pk},
            ),
            fetch_redirect_response=False,
        )
        self.assertEqual(group_campaign.deliveries.count(), 1)
        self.assertEqual(group_campaign.audience, MailingCampaign.Audience.GROUPS)
        self.assertFalse(
            group_campaign.deliveries.exclude(
                recipient_kind=MailingDelivery.RecipientKind.TEACHER
            ).exists()
        )
        self.assertEqual([message.to for message in mail.outbox], [["camille@example.test"]])

        mail.outbox.clear()
        organizer_response = self.client.post(
            reverse("operations:mailing-create"),
            {
                "visit_date": "2026-09-23",
                "family": str(self.family.pk),
                "organizer_subject": "Message pour l’animation",
                "organizer_body_html": "<p>Uniquement pour l’animation.</p>",
                "idempotency_key": "mailing-organizer-only",
                "action": "send_organizers",
            },
        )

        organizer_campaign = MailingCampaign.objects.get(
            idempotency_key="mailing-organizer-only"
        )
        self.assertRedirects(
            organizer_response,
            reverse(
                "operations:mailing-detail",
                kwargs={"campaign_id": organizer_campaign.pk},
            ),
            fetch_redirect_response=False,
        )
        self.assertEqual(organizer_campaign.deliveries.count(), 1)
        self.assertEqual(
            organizer_campaign.audience, MailingCampaign.Audience.ORGANIZERS
        )
        self.assertFalse(
            organizer_campaign.deliveries.exclude(
                recipient_kind=MailingDelivery.RecipientKind.ORGANIZER
            ).exists()
        )
        self.assertEqual([message.to for message in mail.outbox], [["animation@example.test"]])

    def test_single_audience_ignores_missing_addresses_from_the_other_audience(self):
        registration = self.create_and_confirm_registration()
        mail.outbox.clear()
        self.session.organizer_email = ""
        self.session.save(update_fields=("organizer_email",))

        group_response = self.client.post(
            reverse("operations:mailing-create"),
            {
                "subject": "Informations du groupe",
                "body_html": "<p>Message au groupe.</p>",
                "idempotency_key": "group-without-organizer-address",
                "action": "send_groups",
            },
        )

        self.assertEqual(group_response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["camille@example.test"])

        mail.outbox.clear()
        self.session.organizer_email = "animation@example.test"
        self.session.save(update_fields=("organizer_email",))
        registration.teacher.email = ""
        registration.teacher.save(update_fields=("email",))

        organizer_response = self.client.post(
            reverse("operations:mailing-create"),
            {
                "organizer_subject": "Informations de l’animation",
                "organizer_body_html": "<p>Message à l’animation.</p>",
                "idempotency_key": "organizer-without-group-address",
                "action": "send_organizers",
            },
        )

        self.assertEqual(organizer_response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["animation@example.test"])

    def test_final_mailing_requires_confirmation_for_missing_organizer_email(self):
        self.session.organizer_email = ""
        self.session.save(update_fields=("organizer_email",))
        self.create_and_confirm_registration()
        mail.outbox.clear()
        payload = {
            "visit_date": "2026-09-23",
            "family": str(self.family.pk),
            "subject": "Préparer votre venue",
            "body_html": "<p>Accueil à 9 h.</p>",
            "organizer_subject": "Préparer l’accueil des groupes",
            "organizer_body_html": "<p>Consignes pour les animateurs.</p>",
            "idempotency_key": "mailing-adresse-manquante",
            "action": "send",
        }

        response = self.client.post(reverse("operations:mailing-create"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirmez explicitement")
        self.assertFalse(MailingCampaign.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

        payload["confirm_missing"] = "on"
        response = self.client.post(reverse("operations:mailing-create"), payload)

        campaign = MailingCampaign.objects.get(
            idempotency_key="mailing-adresse-manquante"
        )
        self.assertRedirects(
            response,
            reverse("operations:mailing-detail", kwargs={"campaign_id": campaign.pk}),
            fetch_redirect_response=False,
        )
        self.assertEqual(campaign.deliveries.count(), 1)
        self.assertEqual(mail.outbox[0].to, ["camille@example.test"])
