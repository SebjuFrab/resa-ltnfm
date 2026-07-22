from datetime import date, time
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from catalogue.models import Animation, Category, SchoolLevel, Session
from communication.models import EmailLog
from communication.services import (
    schedule_registration_email,
    send_cancellation_email,
    send_confirmation_email,
    send_modification_email,
)
from inscriptions.models import (
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
    Teacher,
)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="organisation@example.test",
    ORGANIZATION_EMAIL="contact@example.test",
    ORGANIZATION_PHONE="02 00 00 00 00",
)
class EmailServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        category = Category.objects.create(name="Nature", slug="nature")
        level = SchoolLevel.objects.create(code="LYCEE", label="Lycée")
        animation = Animation.objects.create(
            title="Le sol vivant",
            slug="sol-vivant",
            short_description="Découvrir le sol.",
            category=category,
            indicative_duration=45,
            instructions="Prévoir des bottes.",
        )
        session = Session.objects.create(
            animation=animation,
            date=date(2026, 9, 23),
            starts_at=time(10),
            ends_at=time(10, 45),
            location="Pôle sols",
            max_capacity=30,
        )
        institution = Institution.objects.create(
            name="Lycée des Champs",
            institution_type=Institution.Type.HIGH_SCHOOL,
            address="1 rue Verte",
            postal_code="35000",
            city="Rennes",
            department="35",
        )
        teacher = Teacher.objects.create(
            institution=institution,
            first_name="Marie",
            last_name="Dupont",
            email="marie@example.test",
            phone="0600000000",
        )
        cls.registration = Registration.objects.create(
            institution=institution,
            teacher=teacher,
            group_name="Seconde A",
            school_level=level,
            student_count=24,
            chaperone_count=2,
            visit_date=date(2026, 9, 23),
            status=Registration.Status.CONFIRMED,
            edit_token_digest="a" * 64,
            confirmed_at=timezone.now(),
        )
        Reservation.objects.create(
            registration=cls.registration,
            session=session,
            student_count=24,
            chaperone_count=2,
        )

    def test_confirmation_sends_text_and_html_and_logs_success(self):
        email_log = send_confirmation_email(
            self.registration,
            edit_url="https://example.test/inscription/lien-secret/",
        )

        self.assertEqual(email_log.status, EmailLog.Status.SENT)
        self.assertIsNotNone(email_log.sent_at)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn("Lycée des Champs", message.body)
        self.assertIn("Pôle sols", message.body)
        self.assertIn("https://example.test/inscription/lien-secret/", message.body)
        self.assertEqual(message.alternatives[0].mimetype, "text/html")
        self.assertTrue(
            RegistrationEvent.objects.filter(
                registration=self.registration,
                event_type=RegistrationEvent.Type.EMAIL_SENT,
            ).exists()
        )

    def test_confirmation_no_longer_requires_an_edit_link_and_includes_total(self):
        send_confirmation_email(self.registration)

        message = mail.outbox[0]
        self.registration.refresh_from_db()
        self.assertIn(self.registration.group_code, message.body)
        self.assertIn("Effectif total : 26", message.body)
        self.assertNotIn("Consulter ou modifier", message.body)

    def test_modification_no_longer_requires_an_edit_link(self):
        send_modification_email(self.registration)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Effectif total : 26", mail.outbox[0].body)
        self.assertNotIn("Consulter ou modifier", mail.outbox[0].body)

    @patch("communication.services.EmailMultiAlternatives.send")
    def test_smtp_failure_is_sanitized_logged_and_not_raised(self, mocked_send):
        edit_url = "https://example.test/inscription/#jeton-ultra-secret"
        mocked_send.side_effect = RuntimeError(
            f"Erreur pour marie@example.test\n{edit_url} jeton-ultra-secret"
        )

        email_log = send_confirmation_email(
            self.registration,
            edit_url=edit_url,
        )

        self.assertEqual(email_log.status, EmailLog.Status.FAILED)
        self.assertNotIn("marie@example.test", email_log.error_summary)
        self.assertNotIn("jeton-ultra-secret", email_log.error_summary)
        self.assertNotIn(edit_url, email_log.error_summary)
        self.assertNotIn("\n", email_log.error_summary)
        self.assertTrue(
            RegistrationEvent.objects.filter(
                registration=self.registration,
                event_type=RegistrationEvent.Type.EMAIL_FAILED,
            ).exists()
        )

    def test_scheduling_waits_until_transaction_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            schedule_registration_email(
                self.registration,
                EmailLog.Kind.CONFIRMATION,
                edit_url="https://example.test/modifier/",
            )
            self.assertEqual(EmailLog.objects.count(), 0)

        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(EmailLog.objects.count(), 1)

    def test_cancellation_email_does_not_contain_an_edit_link(self):
        send_cancellation_email(self.registration)

        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("modifier mon inscription", mail.outbox[0].body.lower())
