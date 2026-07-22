from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from catalogue.models import SchoolLevel
from communication.models import EmailLog, MailingCampaign, MailingDelivery
from inscriptions.models import Institution, Registration, RegistrationEvent, Teacher


@override_settings(DATA_RETENTION_DAYS=30)
class AnonymizationCommandTests(TestCase):
    def setUp(self):
        level = SchoolLevel.objects.create(code="TEST", label="Test")
        self.institution = Institution.objects.create(
            name="Établissement test",
            address="1 rue Test",
            postal_code="35000",
            city="Rennes",
            department="35",
            phone="0102030405",
            administrative_email="direction@example.test",
        )
        self.teacher = Teacher.objects.create(
            institution=self.institution,
            first_name="Marie",
            last_name="Dupont",
            email="marie@example.test",
            phone="0600000000",
        )
        self.registration = Registration.objects.create(
            institution=self.institution,
            teacher=self.teacher,
            group_name="Classe identifiable",
            school_level=level,
            student_count=24,
            visit_date=timezone.localdate() - timedelta(days=60),
            special_needs="Information de santé",
            level_comment="Niveau et dispositif identifiants",
            comment="Commentaire privé",
            status=Registration.Status.CONFIRMED,
            edit_token_digest="d" * 64,
        )
        RegistrationEvent.objects.create(
            registration=self.registration,
            event_type=RegistrationEvent.Type.UPDATED,
            actor_kind=RegistrationEvent.ActorKind.TEACHER,
            changes={"comment": "Commentaire privé"},
        )
        EmailLog.objects.create(
            registration=self.registration,
            kind=EmailLog.Kind.CONFIRMATION,
            recipient="marie@example.test",
            status=EmailLog.Status.SENT,
        )
        campaign = MailingCampaign.objects.create(
            subject="Informations finales",
            body_html="<p>Informations.</p>",
            body_text="Informations.",
        )
        self.teacher_delivery = MailingDelivery.objects.create(
            campaign=campaign,
            recipient_kind=MailingDelivery.RecipientKind.TEACHER,
            recipient=self.teacher.email,
            recipient_name=str(self.teacher),
            registration=self.registration,
            dedupe_key=f"teacher:registration:{self.registration.pk}",
            context_snapshot={
                "registration": {
                    "group_code": self.registration.group_code,
                    "teacher_email": self.teacher.email,
                }
            },
        )
        self.organizer_delivery = MailingDelivery.objects.create(
            campaign=campaign,
            recipient_kind=MailingDelivery.RecipientKind.ORGANIZER,
            recipient="responsable@example.test",
            recipient_name="Responsable",
            dedupe_key="organizer:responsable@example.test",
            context_snapshot={
                "sessions": [
                    {
                        "groups": [
                            {
                                "registration_id": self.registration.pk,
                                "group_code": self.registration.group_code,
                                "teacher_email": self.teacher.email,
                                "total_count": 24,
                            }
                        ],
                        "total_count": 24,
                    }
                ]
            },
        )

    def test_command_redacts_personal_data_and_preserves_counts(self):
        output = StringIO()
        call_command("anonymize_old_registrations", stdout=output)

        self.registration.refresh_from_db()
        self.institution.refresh_from_db()
        self.assertIsNotNone(self.registration.anonymized_at)
        self.assertEqual(self.registration.group_name, "Groupe anonymisé")
        self.assertEqual(self.registration.special_needs, "")
        self.assertEqual(self.registration.level_comment, "")
        self.assertEqual(self.registration.comment, "")
        self.assertEqual(self.registration.student_count, 24)
        self.assertNotEqual(self.registration.teacher_id, self.teacher.pk)
        self.assertFalse(Teacher.objects.filter(pk=self.teacher.pk).exists())
        self.assertEqual(self.institution.administrative_email, "")
        self.assertEqual(
            EmailLog.objects.get(registration=self.registration).recipient,
            "anonymized@example.invalid",
        )
        self.teacher_delivery.refresh_from_db()
        self.organizer_delivery.refresh_from_db()
        self.assertEqual(
            self.teacher_delivery.recipient, "anonymized@example.invalid"
        )
        self.assertEqual(self.teacher_delivery.context_snapshot, {"redacted": True})
        self.assertNotIn(
            "marie@example.test", str(self.organizer_delivery.context_snapshot)
        )
        self.assertEqual(self.organizer_delivery.context_snapshot, {"sessions": []})
        self.assertEqual(
            self.organizer_delivery.recipient, "anonymized@example.invalid"
        )
        self.assertEqual(self.organizer_delivery.recipient_name, "")
        self.assertTrue(
            self.registration.events.filter(
                event_type=RegistrationEvent.Type.ANONYMIZED
            ).exists()
        )
        self.assertNotIn(
            "Commentaire privé",
            str(list(self.registration.events.values_list("changes", flat=True))),
        )

    def test_dry_run_does_not_change_data(self):
        call_command("anonymize_old_registrations", "--dry-run", stdout=StringIO())

        self.registration.refresh_from_db()
        self.assertIsNone(self.registration.anonymized_at)
        self.assertEqual(self.registration.teacher, self.teacher)

    def test_future_visit_is_never_anonymized_from_an_old_creation_date(self):
        future = Registration.objects.create(
            institution=self.institution,
            teacher=self.teacher,
            group_name="Visite future",
            school_level=self.registration.school_level,
            student_count=12,
            visit_date=timezone.localdate() + timedelta(days=60),
            status=Registration.Status.CONFIRMED,
            edit_token_digest="e" * 64,
        )
        Registration.objects.filter(pk=future.pk).update(
            created_at=timezone.now() - timedelta(days=800)
        )

        call_command("anonymize_old_registrations", stdout=StringIO())

        future.refresh_from_db()
        self.assertIsNone(future.anonymized_at)
        self.assertEqual(future.group_name, "Visite future")
