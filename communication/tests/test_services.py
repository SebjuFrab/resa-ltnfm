from datetime import date, time
from unittest.mock import patch

from django.contrib.auth import get_user_model
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
            organizer="Alice Responsable",
            organizer_email="alice.responsable@example.test",
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

    def _assert_branded_message(self, message):
        html_body = message.alternatives[0].content
        self.assertIn("cid:ltnfm-logo", html_body)
        self.assertIn("#f3b709", html_body)
        self.assertIn("#14ad88", html_body)
        self.assertEqual(message.mixed_subtype, "related")
        self.assertTrue(
            any(
                attachment.get_content_type() == "image/jpeg"
                for attachment in message.attachments
            )
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
        self.assertIn("Contact enseignant·e : Dupont Marie — marie@example.test", message.body)
        self.assertIn("Alice Responsable", message.body)
        self.assertNotIn("alice.responsable@example.test", message.body)
        self.assertNotIn("Référence", message.body)
        self.assertNotIn(str(self.registration.reference), message.body)
        self.assertNotIn("Consulter ou modifier", message.body)
        html_body = message.alternatives[0].content
        self.assertIn("cid:ltnfm-logo", html_body)
        self.assertIn("#f3b709", html_body)
        self.assertIn("#14ad88", html_body)
        self.assertIn("Contact enseignant·e", html_body)
        self.assertNotIn("alice.responsable@example.test", html_body)
        self.assertNotIn(str(self.registration.reference), html_body)
        self._assert_branded_message(message)

    def test_confirmation_copies_the_staff_user_who_created_the_registration(self):
        creator = get_user_model().objects.create_user(
            username="creator",
            email="creator@example.test",
        )
        RegistrationEvent.objects.create(
            registration=self.registration,
            event_type=RegistrationEvent.Type.CREATED,
            actor_kind=RegistrationEvent.ActorKind.STAFF,
            actor_user=creator,
        )

        send_confirmation_email(self.registration)

        self.assertEqual(mail.outbox[0].to, ["marie@example.test"])
        self.assertEqual(mail.outbox[0].cc, ["creator@example.test"])

    def test_teacher_recap_matches_word_comments_for_each_registration_email(self):
        self.registration.group_name = "Nom de groupe à ne plus afficher"
        self.registration.level_comment = "Classe mixte <niveau>"
        self.registration.save(update_fields=("group_name", "level_comment"))
        for send in (send_confirmation_email, send_modification_email, send_cancellation_email):
            with self.subTest(send=send.__name__):
                send(self.registration)
                message = mail.outbox[-1]
                html = message.alternatives[0].content
                for body in (message.body, html):
                    self.assertIn("Classe", body)
                    self.assertIn("Lycée", body)
                    self.assertIn("Code du groupe", body)
                    self.assertEqual(body.count(self.registration.group_code), 1)
                    self.assertNotIn(self.registration.group_name, body)
                    self.assertIn("Contact enseignant·e", body)
                    self.assertIn("Dupont Marie", body)
                    self.assertIn("marie@example.test", body)
                    self.assertNotIn("alice.responsable@example.test", body)
                    self.assertIn("Louise Le Moing", body)
                    self.assertIn("06 22 68 23 91", body)
                self.assertIn("font-size:12px;line-height:1.35;", html)
                self.assertIn("Classe mixte &lt;niveau&gt;", html)
                self.assertNotIn("<niveau>", html)

    def test_confirmation_includes_visit_instructions_from_word(self):
        send_confirmation_email(self.registration)
        message = mail.outbox[-1]
        for body in (message.body, message.alternatives[0].content):
            for instruction in (
                "Imprimez ce courriel", "billet d’entrée", "15 minutes",
                "places non occupées", "groupes en retard", "sécurité et de courtoisie",
                "lundi 21 septembre", "pas par courriel", "À bientôt à Retiers",
            ):
                self.assertIn(instruction, body)

    def test_organizer_with_only_an_email_is_not_exposed_in_teacher_mail(self):
        session = self.registration.reservations.get().session
        session.organizer = ""
        session.save(update_fields=("organizer",))
        send_confirmation_email(self.registration)
        message = mail.outbox[-1]
        for body in (message.body, message.alternatives[0].content):
            self.assertNotIn("alice.responsable@example.test", body)
            self.assertNotIn("Responsable de l’animation", body)

    def test_confirmation_does_not_duplicate_the_teacher_address_in_cc(self):
        creator = get_user_model().objects.create_user(
            username="creator",
            email="marie@example.test",
        )
        RegistrationEvent.objects.create(
            registration=self.registration,
            event_type=RegistrationEvent.Type.CREATED,
            actor_kind=RegistrationEvent.ActorKind.STAFF,
            actor_user=creator,
        )

        send_confirmation_email(self.registration)

        self.assertEqual(mail.outbox[0].cc, [])

    def test_explicit_staff_sender_is_copied_for_every_registration_email(self):
        staff = get_user_model().objects.create_user(
            username="sender",
            email="sender@example.test",
        )

        for sender in (
            send_confirmation_email,
            send_modification_email,
            send_cancellation_email,
        ):
            with self.subTest(sender=sender.__name__):
                mail.outbox.clear()
                sender(self.registration, cc_email=staff.email)
                self.assertEqual(mail.outbox[0].to, ["marie@example.test"])
                self.assertEqual(mail.outbox[0].cc, ["sender@example.test"])

    def test_modification_no_longer_requires_an_edit_link(self):
        creator = get_user_model().objects.create_user(
            username="creator",
            email="creator@example.test",
        )
        RegistrationEvent.objects.create(
            registration=self.registration,
            event_type=RegistrationEvent.Type.CREATED,
            actor_kind=RegistrationEvent.ActorKind.STAFF,
            actor_user=creator,
        )
        send_modification_email(self.registration)

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn("Effectif total : 26", message.body)
        self.assertIn("Contact enseignant·e : Dupont Marie — marie@example.test", message.body)
        self.assertNotIn("Référence", message.body)
        self.assertNotIn(str(self.registration.reference), message.body)
        self.assertNotIn("Consulter ou modifier", message.body)
        self.assertEqual(message.cc, [])
        self._assert_branded_message(message)

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
        staff = get_user_model().objects.create_user(
            username="validator",
            email="validator@example.test",
        )
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            schedule_registration_email(
                self.registration,
                EmailLog.Kind.CONFIRMATION,
                edit_url="https://example.test/modifier/",
                initiated_by=staff,
            )
            self.assertEqual(EmailLog.objects.count(), 0)

        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(EmailLog.objects.count(), 1)
        self.assertEqual(mail.outbox[0].cc, ["validator@example.test"])

    def test_all_teacher_messages_share_louises_contact_and_keep_sender_cc_and_reply(self):
        staff = get_user_model().objects.create_user(
            username="personal-contact",
            first_name="Camille",
            last_name="Durand",
            email="camille.durand@example.test",
        )
        footers = []
        for kind in EmailLog.Kind.values:
            with self.subTest(kind=kind):
                with self.captureOnCommitCallbacks(execute=True):
                    schedule_registration_email(self.registration, kind, initiated_by=staff)
                message = mail.outbox[-1]
                html = message.alternatives[0].content
                self.assertIn(
                    "Votre contact pour le salon : Louise Le Moing — 06 22 68 23 91",
                    message.body,
                )
                self.assertIn("Louise Le Moing", html)
                self.assertIn("tel:+33622682391", html)
                self.assertNotIn("camille.durand@example.test", html)
                self.assertNotIn("contact@example.test", html)
                self.assertNotIn("02 00 00 00 00", message.body)
                self.assertEqual(message.cc, [staff.email])
                self.assertEqual(message.reply_to, [staff.email])
                footers.append(html.split("Votre contact pour le salon", 1)[1].split("</tr>")[0])
        self.assertEqual(footers[0], footers[1])
        self.assertEqual(footers[0], footers[2])

    def test_sender_copy_and_reply_are_frozen_before_transaction_commit(self):
        staff = get_user_model().objects.create_user(
            username="frozen-contact", first_name="Camille", email="before@example.test",
        )
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            schedule_registration_email(
                self.registration, EmailLog.Kind.MODIFICATION, initiated_by=staff
            )
        staff.first_name = "Changed"
        staff.email = "after@example.test"
        staff.save()
        callbacks[0]()
        message = mail.outbox[-1]
        self.assertIn("Louise Le Moing — 06 22 68 23 91", message.body)
        self.assertEqual(message.cc, ["before@example.test"])
        self.assertEqual(message.reply_to, ["before@example.test"])

    def test_sender_with_recipient_address_does_not_duplicate_copy(self):
        staff = get_user_model().objects.create_user(
            username="same-address", first_name="Contact", email="MARIE@example.test",
        )
        with self.captureOnCommitCallbacks(execute=True):
            schedule_registration_email(
                self.registration, EmailLog.Kind.MODIFICATION, initiated_by=staff
            )
        self.assertEqual(mail.outbox[0].cc, [])
        self.assertEqual(mail.outbox[0].reply_to, ["MARIE@example.test"])
        self.assertIn("Louise Le Moing — 06 22 68 23 91", mail.outbox[0].body)

    def test_no_sender_address_keeps_teacher_contact_and_organization_reply_fallback(self):
        staff = get_user_model().objects.create_user(username="no-address")
        with self.captureOnCommitCallbacks(execute=True):
            schedule_registration_email(
                self.registration, EmailLog.Kind.MODIFICATION, initiated_by=staff
            )
        self.assertIn("Louise Le Moing — 06 22 68 23 91", mail.outbox[0].body)
        self.assertNotIn("Organisation du salon :", mail.outbox[0].body)
        self.assertNotIn("02 00 00 00 00", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].reply_to, ["contact@example.test"])

    def test_cancellation_email_does_not_contain_an_edit_link(self):
        send_cancellation_email(self.registration)

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn("Contact enseignant·e : Dupont Marie — marie@example.test", message.body)
        self.assertNotIn("Référence", message.body)
        self.assertNotIn(str(self.registration.reference), message.body)
        self.assertNotIn("modifier mon inscription", message.body.lower())
        self._assert_branded_message(message)
