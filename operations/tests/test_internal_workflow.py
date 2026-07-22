from datetime import date, time

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from catalogue.models import Animation, Category, SchoolLevel, Session
from communication.models import EmailLog, MailingCampaign, MailingDelivery
from inscriptions.models import (
    GroupFamily,
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
)
from operations.forms import StaffRegistrationForm


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
            indicative_duration=60,
        )
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
            {f"session_{self.session.pk}": "on"},
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

    def test_staff_update_resizes_reservations_and_notifies_teacher(self):
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
        reservation = registration.reservations.get(status=Reservation.Status.ACTIVE)
        self.assertEqual(registration.total_participant_count, 23)
        self.assertEqual(reservation.total_participant_count, 23)
        self.assertEqual(EmailLog.objects.filter(kind="MODIFICATION").count(), 1)
        self.assertEqual(len(mail.outbox), 1)

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

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                (
                    reverse(
                        "operations:registration-planning",
                        kwargs={"reference": registration.reference},
                    )
                    + "?reschedule=1"
                ),
                {f"session_{next_day_session.pk}": "on"},
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

    def test_final_mailing_sends_individual_teacher_and_organizer_messages(self):
        self.create_and_confirm_registration()
        mail.outbox.clear()

        response = self.client.get(reverse("operations:mailing-create"))
        self.assertEqual(response.context["preview"].teacher_count, 1)
        self.assertEqual(response.context["preview"].organizer_count, 1)

        response = self.client.post(
            reverse("operations:mailing-create"),
            {
                "visit_date": "2026-09-23",
                "family": str(self.family.pk),
                "subject": "Préparer votre venue",
                "body_html": "<p><strong>Accueil à 9 h.</strong></p><script>alert(1)</script>",
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
        self.assertEqual(campaign.deliveries.count(), 2)
        self.assertFalse(
            campaign.deliveries.exclude(status=MailingDelivery.Status.SENT).exists()
        )
        self.assertCountEqual(
            [message.to[0] for message in mail.outbox],
            ["camille@example.test", "animation@example.test"],
        )

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
