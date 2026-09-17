import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from catalogue.models import Session
from communication.models import EmailLog
from inscriptions.models import Registration, RegistrationEvent, Reservation
from operations.bulk_confirmation import MAX_AGE, SELECTION_SALT, SEND_SALT

from .factories import create_operational_data


class BulkConfirmationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_superuser(
            username="bulk-admin", email="admin@example.test", password="secret"
        )
        cls.data = create_operational_data()
        cls.confirmed = cls.data["registration"]
        cls.draft = cls.make_draft()
        cls.empty_draft = cls.make_draft(with_reservation=False)

    @classmethod
    def make_draft(cls, *, with_reservation=True):
        registration = Registration.objects.create(
            institution=cls.confirmed.institution,
            teacher=cls.confirmed.teacher,
            school_level=cls.confirmed.school_level,
            visit_date=cls.confirmed.visit_date,
            student_count=24,
            chaperone_count=2,
            edit_token_digest=uuid.uuid4().hex,
            draft_expires_at=timezone.now() + timezone.timedelta(hours=1),
        )
        if with_reservation:
            Reservation.objects.create(
                registration=registration,
                session=cls.data["session"],
                student_count=24,
                chaperone_count=2,
            )
        return registration

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse("operations:bulk-confirmation")
        self.send_url = reverse("operations:bulk-confirmation-send")

    def form_data(self):
        response = self.client.get(self.url)
        return {
            "selection": response.context["form"]["selection"].value(),
            "subject": "Votre visite au salon",
            "message_text": "Rendez-vous mercredi !\nMerci de prévoir des bottes.",
            "confirm": "on",
            "action": "start",
        }

    def prepare(self, **changes):
        data = self.form_data()
        data.update(changes)
        return self.client.post(self.url, data)

    def send(self, token, registration=None):
        return self.client.post(
            self.send_url,
            {
                "token": token,
                "reference": str((registration or self.draft).reference),
            },
        )

    def test_group_list_links_to_batch_and_get_never_changes_or_sends(self):
        response = self.client.get(reverse("operations:registration-list"))
        self.assertContains(response, self.url)
        response = self.client.get(self.url)
        self.assertEqual(response.context["ready_count"], 1)
        self.assertEqual(response.context["excluded_count"], 1)
        self.assertContains(response, self.draft.group_code)
        self.assertContains(response, "Aucune animation choisie")
        self.assertContains(response, self.staff.email)
        self.assertNotContains(response, self.confirmed.group_code)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Registration.Status.DRAFT)
        self.assertFalse(EmailLog.objects.exists())

    def test_includes_all_pages_and_ignores_list_filters_but_excludes_cancelled(self):
        for _ in range(51):
            self.make_draft()
        Registration.objects.filter(pk=self.confirmed.pk).update(status="CANCELLED")
        response = self.client.get(self.url, {"page": 2, "q": "not-a-group"})
        self.assertEqual(response.context["ready_count"], 52)
        self.assertEqual(response.context["excluded_count"], 1)
        plan = signing.loads(response.context["form"]["selection"].value(), salt=SELECTION_SALT)
        self.assertEqual(len(plan["targets"]), 53)
        self.assertNotIn(str(self.confirmed.reference), plan["targets"])

    def test_preview_uses_custom_text_and_subject_without_sending(self):
        response = self.prepare(
            action="preview", confirm="", message_text="Bonjour <script>x</script>"
        )
        self.assertEqual(response.status_code, 200)
        html = response.context["preview_html"]
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn(self.data["animation"].title, html)
        self.assertIn(self.draft.group_code, html)
        self.assertNotIn("Imprimez ce courriel", html)
        self.assertContains(response, "Votre visite au salon")
        self.assertFalse(EmailLog.objects.exists())

    def test_start_requires_confirmation_and_valid_subject_and_body(self):
        for changes in (
            {"confirm": ""},
            {"subject": "a\r\nBcc: other@example.test"},
            {"message_text": ""},
        ):
            with self.subTest(changes=changes):
                response = self.prepare(**changes)
                self.assertTrue(response.context["form"].errors)
                self.assertNotIn("send_token", response.context)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Registration.Status.DRAFT)

    def test_confirm_sends_immediately_with_admin_cc_and_audit(self):
        prepared = self.prepare()
        token = prepared.context["send_token"]
        self.assertFalse(EmailLog.objects.exists())
        # SMTP must see the committed confirmation, not an on_commit callback
        # queued until the end of the entire batch.
        response = self.send(token)
        self.assertEqual(response.json()["status"], "sent")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Registration.Status.CONFIRMED)
        self.assertIsNotNone(self.draft.confirmed_at)
        self.assertIsNone(self.draft.draft_expires_at)
        event = self.draft.events.get(event_type=RegistrationEvent.Type.CONFIRMED)
        self.assertEqual(event.actor_user, self.staff)
        self.assertEqual(event.actor_kind, RegistrationEvent.ActorKind.STAFF)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.draft.teacher.email])
        self.assertEqual(message.cc, [self.staff.email])
        self.assertEqual(message.message()["Cc"], self.staff.email)
        self.assertEqual(message.reply_to, [self.staff.email])
        self.assertEqual(message.subject, "Votre visite au salon")
        self.assertIn("Rendez-vous mercredi !", message.body)
        self.assertIn("Rendez-vous mercredi !", message.alternatives[0].content)
        self.assertIn(self.data["animation"].title, message.body)
        self.assertIn(self.draft.group_code, message.body)
        self.assertNotIn("Imprimez ce courriel", message.body)
        self.empty_draft.refresh_from_db()
        self.assertEqual(self.empty_draft.status, Registration.Status.DRAFT)

    def test_repeated_request_or_second_batch_cannot_send_twice(self):
        first = self.prepare().context["send_token"]
        second = self.prepare().context["send_token"]
        self.assertEqual(self.send(first).json()["status"], "sent")
        self.assertEqual(self.send(first).json()["status"], "skipped")
        self.assertEqual(self.send(second).json()["status"], "skipped")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.draft.events.filter(event_type="CONFIRMED").count(), 1)

    def test_new_draft_created_after_preview_is_not_silently_included(self):
        data = self.form_data()
        new_draft = self.make_draft()
        response = self.client.post(self.url, data)
        token = response.context["send_token"]
        self.assertEqual(self.send(token, new_draft).status_code, 400)
        self.assertEqual(self.send(token, self.confirmed).status_code, 400)
        self.assertEqual(self.send(token, self.empty_draft).status_code, 400)

    def test_changed_draft_is_not_confirmed_without_another_review(self):
        token = self.prepare().context["send_token"]
        self.draft.comment = "Modification dans un autre onglet"
        self.draft.save()
        response = self.send(token)
        self.assertEqual(response.json()["status"], "skipped")
        self.assertIn("Modifié depuis", response.json()["message"])
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Registration.Status.DRAFT)
        self.assertFalse(EmailLog.objects.exists())

    def test_cancelled_draft_is_not_confirmed_or_contacted(self):
        token = self.prepare().context["send_token"]
        Registration.objects.filter(pk=self.draft.pk).update(status="CANCELLED")
        self.assertEqual(self.send(token).json()["status"], "skipped")
        self.assertFalse(EmailLog.objects.exists())

    def test_failure_of_one_program_does_not_prevent_other_groups(self):
        other = self.make_draft()
        token = self.prepare().context["send_token"]
        self.draft.reservations.update(status=Reservation.Status.CANCELLED)
        self.assertEqual(self.send(token).json()["status"], "skipped")
        self.assertEqual(self.send(token, other).json()["status"], "sent")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Registration.Status.DRAFT)
        self.assertEqual(len(mail.outbox), 1)

    def test_closed_session_keeps_group_as_draft(self):
        token = self.prepare().context["send_token"]
        Session.objects.filter(pk=self.data["session"].pk).update(status=Session.Status.CLOSED)
        self.assertEqual(self.send(token).json()["status"], "skipped")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Registration.Status.DRAFT)

    def test_over_capacity_warns_but_does_not_block_confirmation(self):
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["over_capacity"]), 1)
        self.assertContains(response, "52 personnes / 30 places")
        token = self.prepare().context["send_token"]
        self.assertEqual(self.send(token).json()["status"], "sent")

    def test_smtp_failure_is_reported_without_reverting_or_resending(self):
        token = self.prepare().context["send_token"]
        with patch(
            "communication.services.EmailMultiAlternatives.send", side_effect=OSError("SMTP")
        ):
            self.assertEqual(self.send(token).json()["status"], "failed")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Registration.Status.CONFIRMED)
        self.assertEqual(self.draft.email_logs.get().status, EmailLog.Status.FAILED)
        self.assertEqual(self.send(token).json()["status"], "skipped")
        self.assertEqual(self.draft.email_logs.count(), 1)

    def test_capacity_warning_includes_expired_holds_that_will_be_reactivated(self):
        Registration.objects.filter(pk=self.draft.pk).update(
            draft_expires_at=timezone.now() - timezone.timedelta(hours=1)
        )
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["over_capacity"]), 1)
        self.assertContains(response, "52 personnes / 30 places")

    def test_empty_batch_cannot_be_started(self):
        data = self.form_data()
        self.draft.reservations.update(status=Reservation.Status.CANCELLED)
        response = self.client.post(self.url, data)
        self.assertNotIn("send_token", response.context)
        self.assertEqual(response.context["ready_count"], 0)
        self.assertFalse(EmailLog.objects.exists())

    def test_missing_admin_email_blocks_start_and_send(self):
        token = self.prepare().context["send_token"]
        for email in ("", "not-an-address"):
            with self.subTest(email=email):
                self.staff.email = email
                self.staff.save()
                response = self.prepare()
                self.assertContains(response, "L’envoi est bloqué")
                self.assertNotIn("send_token", response.context)
                self.assertEqual(self.send(token).status_code, 400)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Registration.Status.DRAFT)
        self.assertFalse(EmailLog.objects.exists())

    def test_missing_recipient_is_excluded_and_rechecked_before_send(self):
        token = self.prepare().context["send_token"]
        teacher = self.draft.teacher
        teacher.email = ""
        teacher.save()
        self.assertEqual(self.send(token).json()["status"], "skipped")
        response = self.client.get(self.url)
        self.assertEqual(response.context["ready_count"], 0)
        self.assertContains(response, "Adresse du responsable absente ou invalide")
        self.assertFalse(EmailLog.objects.exists())

    def test_admin_already_recipient_is_not_duplicated(self):
        self.staff.email = self.draft.teacher.email
        self.staff.save()
        token = self.prepare().context["send_token"]
        self.assertEqual(self.send(token).json()["status"], "sent")
        self.assertEqual(mail.outbox[0].to, [self.staff.email])
        self.assertEqual(mail.outbox[0].cc, [])

    def test_expired_or_tampered_tokens_are_rejected(self):
        data = self.form_data()
        token = self.prepare().context["send_token"]
        self.assertEqual(self.send(token + "x").status_code, 400)
        data["selection"] += "x"
        self.assertEqual(self.client.post(self.url, data).status_code, 400)
        data = self.form_data()
        with patch(
            "django.core.signing.time.time", return_value=timezone.now().timestamp() + MAX_AGE + 1
        ):
            self.assertEqual(self.client.post(self.url, data).status_code, 400)
            self.assertEqual(self.send(token).status_code, 400)

    def test_selection_and_send_plan_are_bound_to_current_admin(self):
        data = self.form_data()
        token = self.prepare().context["send_token"]
        other = get_user_model().objects.create_superuser(
            username="another-admin", email="other@example.test", password="secret"
        )
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url, data).status_code, 400)
        self.assertEqual(self.send(token).status_code, 400)
        self.assertFalse(EmailLog.objects.exists())

    def test_permission_csrf_and_method_checks(self):
        token = self.prepare().context["send_token"]
        self.assertEqual(self.client.get(self.send_url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        self.assertEqual(
            csrf_client.post(
                self.send_url,
                {
                    "token": token,
                    "reference": str(self.draft.reference),
                },
            ).status_code,
            403,
        )
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.send(token).status_code, 302)
        reader = get_user_model().objects.create_user(username="reader", is_staff=True)
        self.client.force_login(reader)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.send(token).status_code, 403)

    def test_run_plan_contains_only_reviewed_message_and_eligible_groups(self):
        response = self.prepare()
        plan = signing.loads(response.context["send_token"], salt=SEND_SALT)
        self.assertEqual(list(plan["targets"]), [str(self.draft.reference)])
        self.assertEqual(plan["subject"], "Votre visite au salon")
        self.assertEqual(plan["user_id"], self.staff.pk)
        self.assertContains(response, "bulk-confirmation.js")
        self.assertFalse(EmailLog.objects.exists())
