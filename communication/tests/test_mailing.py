from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from catalogue.models import Animation, Category, SchoolLevel, Session
from communication.mailing import (
    create_and_send_mailing,
    create_mailing_campaign,
    preview_mailing_recipients,
    send_mailing_campaign,
)
from communication.models import MailingCampaign, MailingDelivery
from communication.rich_text import rich_html_to_text, sanitize_rich_html
from inscriptions.models import (
    GroupFamily,
    Institution,
    Registration,
    Reservation,
    Teacher,
)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="organisation@example.test",
    ORGANIZATION_EMAIL="contact@example.test",
    ORGANIZATION_PHONE="02 00 00 00 00",
)
class MailingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="frab", password="secret")
        category = Category.objects.create(name="Nature", slug="nature")
        cls.level = SchoolLevel.objects.create(code="LYCEE", label="Lycée")
        cls.family = GroupFamily.objects.create(
            name="Lycées", slug="lycees", sort_order=1
        )
        cls.other_family = GroupFamily.objects.create(
            name="Collèges", slug="colleges", sort_order=2
        )
        animation = Animation.objects.create(
            title="Le sol vivant",
            slug="sol-vivant",
            short_description="Découvrir le sol.",
            category=category,
            indicative_duration=45,
        )
        other_animation = Animation.objects.create(
            title="Les graines",
            slug="graines",
            short_description="Découvrir les graines.",
            category=category,
            indicative_duration=30,
        )
        cls.first_session = Session.objects.create(
            animation=animation,
            date=date(2026, 9, 23),
            starts_at=time(10),
            ends_at=time(10, 45),
            location="Pôle sols",
            max_capacity=60,
            organizer="Équipe sols",
            organizer_email="responsable@example.test",
        )
        cls.second_session = Session.objects.create(
            animation=other_animation,
            date=date(2026, 9, 24),
            starts_at=time(11),
            ends_at=time(11, 30),
            location="Pôle graines",
            max_capacity=60,
            organizer="Équipe graines",
            organizer_email="RESPONSABLE@example.test",
        )
        cls.first_registration = cls._registration(
            suffix="a",
            group_code="TRUFFE",
            visit_date=date(2026, 9, 23),
            family=cls.family,
            session=cls.first_session,
            students=24,
            chaperones=2,
        )
        cls.second_registration = cls._registration(
            suffix="b",
            group_code="CAROTTE",
            visit_date=date(2026, 9, 24),
            family=cls.other_family,
            session=cls.second_session,
            students=18,
            chaperones=3,
        )
        cls._registration(
            suffix="draft",
            group_code="NAVET",
            visit_date=date(2026, 9, 23),
            family=cls.family,
            session=cls.first_session,
            students=10,
            chaperones=1,
            status=Registration.Status.DRAFT,
        )

    @classmethod
    def _registration(
        cls,
        *,
        suffix,
        group_code,
        visit_date,
        family,
        session,
        students,
        chaperones,
        status=Registration.Status.CONFIRMED,
    ):
        institution = Institution.objects.create(
            name=f"Établissement {suffix}",
            institution_type=Institution.Type.HIGH_SCHOOL,
            address="1 rue Verte",
            postal_code="35000",
            city="Rennes",
            department="35",
        )
        teacher = Teacher.objects.create(
            institution=institution,
            first_name=f"Prénom {suffix}",
            last_name=f"Nom {suffix}",
            email=f"prof-{suffix}@example.test",
            phone="0600000000",
        )
        registration = Registration.objects.create(
            institution=institution,
            teacher=teacher,
            group_name=f"Groupe {suffix}",
            group_code=group_code,
            family=family,
            school_level=cls.level,
            student_count=students,
            chaperone_count=chaperones,
            visit_date=visit_date,
            status=status,
            draft_expires_at=(timezone.now() if status == Registration.Status.DRAFT else None),
            edit_token_digest=(suffix[0] * 64 if len(suffix) == 1 else "d" * 63 + "1"),
            confirmed_at=(timezone.now() if status == Registration.Status.CONFIRMED else None),
        )
        Reservation.objects.create(
            registration=registration,
            session=session,
            student_count=students,
            chaperone_count=chaperones,
        )
        return registration

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

    def test_preview_counts_confirmed_teachers_and_deduplicated_organizer(self):
        preview = preview_mailing_recipients()

        self.assertEqual(preview.teacher_count, 2)
        self.assertEqual(preview.organizer_count, 1)
        self.assertEqual(preview.total_count, 3)
        self.assertEqual(preview.missing_teacher_email_count, 0)
        self.assertEqual(preview.missing_organizer_email_count, 0)

    def test_preview_filters_by_date_and_family(self):
        by_date = preview_mailing_recipients(visit_date="2026-09-23")
        by_family = preview_mailing_recipients(family=self.other_family)
        by_slug = preview_mailing_recipients(family="lycees")

        self.assertEqual((by_date.teacher_count, by_date.organizer_count), (1, 1))
        self.assertEqual((by_family.teacher_count, by_family.organizer_count), (1, 1))
        self.assertEqual((by_slug.teacher_count, by_slug.organizer_count), (1, 1))

    def test_each_location_manager_receives_every_group_at_that_location(self):
        second_manager_session = Session.objects.create(
            animation=self.first_session.animation,
            date=date(2026, 9, 23),
            starts_at=time(14),
            ends_at=time(14, 45),
            location="Pôle sols",
            max_capacity=60,
            organizer="Deuxième responsable sols",
            organizer_email="autre-responsable@example.test",
        )
        third_registration = self._registration(
            suffix="c",
            group_code="POIREAU",
            visit_date=date(2026, 9, 23),
            family=self.family,
            session=second_manager_session,
            students=15,
            chaperones=2,
        )

        campaign = create_mailing_campaign(
            organizer_subject="Organisation du lieu",
            organizer_body_html="<p>Récapitulatif.</p>",
            visit_date=date(2026, 9, 23),
            recipient_kinds=(MailingDelivery.RecipientKind.ORGANIZER,),
        )

        self.assertEqual(campaign.deliveries.count(), 2)
        for delivery in campaign.deliveries.all():
            self.assertEqual(delivery.context_snapshot["location"], "Pôle sols")
            self.assertEqual(delivery.context_snapshot["group_count"], 2)
            group_codes = {
                group["group_code"]
                for session in delivery.context_snapshot["sessions"]
                for group in session["groups"]
            }
            self.assertEqual(
                group_codes,
                {self.first_registration.group_code, third_registration.group_code},
            )

    def test_location_contact_can_come_from_another_session_without_groups(self):
        self.first_session.organizer = ""
        self.first_session.organizer_email = ""
        self.first_session.save(update_fields=("organizer", "organizer_email"))
        Session.objects.create(
            animation=self.first_session.animation,
            date=date(2026, 9, 23),
            starts_at=time(16),
            ends_at=time(16, 45),
            location="Pôle sols",
            max_capacity=60,
            organizer="Responsable du lieu",
            organizer_email="lieu@example.test",
        )

        campaign = create_mailing_campaign(
            organizer_subject="Organisation du lieu",
            organizer_body_html="<p>Récapitulatif.</p>",
            visit_date=date(2026, 9, 23),
            recipient_kinds=(MailingDelivery.RecipientKind.ORGANIZER,),
        )

        delivery = campaign.deliveries.get()
        self.assertEqual(delivery.recipient, "lieu@example.test")
        self.assertEqual(delivery.context_snapshot["group_count"], 1)
        self.assertEqual(
            delivery.context_snapshot["sessions"][0]["groups"][0]["group_code"],
            self.first_registration.group_code,
        )

    def test_default_preview_excludes_other_editions_and_anonymized_groups(self):
        Registration.objects.filter(pk=self.first_registration.pk).update(
            visit_date=date(2024, 9, 25)
        )
        Registration.objects.filter(pk=self.second_registration.pk).update(
            anonymized_at=timezone.now()
        )

        preview = preview_mailing_recipients()

        self.assertEqual(preview.total_count, 0)

    def test_rich_text_sanitizer_keeps_formatting_and_removes_active_content(self):
        cleaned = sanitize_rich_html(
            '<p style="color:red" onclick="evil()">Bonjour <strong>à tous</strong>'
            '<script>alert(1)</script><a href="javascript:evil()">mauvais</a>'
            '<a href="https://example.test" target="_blank">bon lien</a></p>'
        )

        self.assertEqual(
            cleaned,
            '<p>Bonjour <strong>à tous</strong><a>mauvais</a>'
            '<a href="https://example.test">bon lien</a></p>',
        )
        self.assertEqual(rich_html_to_text(cleaned), "Bonjour à tousmauvaisbon lien")

    def test_create_campaign_freezes_sanitized_content_and_recipients(self):
        campaign = create_mailing_campaign(
            subject=" Informations finales ",
            body_html="<h2>Bienvenue</h2><p>Rendez-vous <em>à 9 h</em>.</p>",
            organizer_subject=" Consignes pour les animations ",
            organizer_body_html=(
                "<p>Préparez l’accueil des <strong>groupes attendus</strong>.</p>"
            ),
            created_by=self.user,
            visit_date=date(2026, 9, 23),
            family=self.family,
        )

        self.assertEqual(campaign.subject, "Informations finales")
        self.assertEqual(campaign.organizer_subject, "Consignes pour les animations")
        self.assertEqual(campaign.family_filter, str(self.family.pk))
        self.assertEqual(campaign.family_label, "Lycées")
        self.assertEqual(campaign.deliveries.count(), 2)
        teacher = campaign.deliveries.get(
            recipient_kind=MailingDelivery.RecipientKind.TEACHER
        )
        self.assertEqual(teacher.registration, self.first_registration)
        self.assertEqual(
            teacher.context_snapshot["registration"]["total_count"], 26
        )
        self.assertIn("Bienvenue", campaign.body_text)
        self.assertIn("groupes attendus", campaign.organizer_body_text)

    def test_send_is_individual_personalized_and_idempotent(self):
        personalized_body = (
            "<p>Variables : {{ prenom }}|{{ nom }}|{{ nombre_inscrits }}</p>"
            "<p>Programme variable : {{ programme }}</p>"
        )
        organizer_body = (
            "<p>Message animation : {{ nom }}|{{ nombre_inscrits }}</p>"
            "<p>Planning animation : {{ programme }}</p>"
        )
        first = create_and_send_mailing(
            subject="Dernières informations pour votre groupe",
            body_html=personalized_body,
            organizer_subject="Consignes pour votre animation",
            organizer_body_html=organizer_body,
            created_by=self.user,
            idempotency_key="mailing-final-2026",
        )

        self.assertEqual(first.campaign.status, MailingCampaign.Status.SENT)
        self.assertEqual(first.sent_count, 3)
        self.assertEqual(first.failed_count, 0)
        self.assertEqual(len(mail.outbox), 3)
        self.assertTrue(all(len(message.to) == 1 for message in mail.outbox))
        teacher_message = next(
            message for message in mail.outbox if message.to == ["prof-a@example.test"]
        )
        organizer_message = next(
            message
            for message in mail.outbox
            if message.to[0].casefold() == "responsable@example.test"
        )
        self._assert_branded_message(teacher_message)
        self._assert_branded_message(organizer_message)
        self.assertEqual(
            teacher_message.subject, "Dernières informations pour votre groupe"
        )
        self.assertEqual(
            organizer_message.subject, "Consignes pour votre animation"
        )
        self.assertIn(self.first_registration.group_code, teacher_message.body)
        self.assertIn("Effectif total : 26", teacher_message.body)
        self.assertIn("Email de contact : prof-a@example.test", teacher_message.body)
        self.assertIn("Organisation du salon : contact@example.test", teacher_message.body)
        self.assertIn("Variables : Prénom a|Nom a|26", teacher_message.body)
        self.assertNotIn("Message animation", teacher_message.body)
        self.assertIn(
            "23/09/2026 · 10:00–10:45 · Le sol vivant — Pôle sols",
            teacher_message.body,
        )
        self.assertIn(self.first_registration.group_code, organizer_message.body)
        self.assertIn(self.second_registration.group_code, organizer_message.body)
        self.assertIn(
            "votre adresse de contact : responsable@example.test",
            organizer_message.body.casefold(),
        )
        self.assertIn(
            "Organisation du salon : contact@example.test",
            organizer_message.body,
        )
        self.assertIn(
            "Message animation : Équipe graines, Équipe sols|47",
            organizer_message.body,
        )
        self.assertNotIn("Variables : Prénom", organizer_message.body)
        self.assertIn(
            "24/09/2026 · 11:00–11:30 · Les graines — Pôle graines",
            organizer_message.body,
        )
        self.assertNotIn("{{ prenom }}", teacher_message.body)
        teacher_html = teacher_message.alternatives[0].content
        self.assertLess(
            teacher_html.index("Votre programme"),
            teacher_html.index("Récapitulatif de votre groupe"),
        )
        self.assertLess(
            teacher_message.body.index("Programme :"),
            teacher_message.body.index("Récapitulatif de votre groupe"),
        )

        second = create_and_send_mailing(
            subject="Dernières informations pour votre groupe",
            body_html=personalized_body,
            organizer_subject="Consignes pour votre animation",
            organizer_body_html=organizer_body,
            created_by=self.user,
            idempotency_key="mailing-final-2026",
        )

        self.assertEqual(MailingCampaign.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(second.skipped_count, 3)

    def test_campaign_can_target_each_audience_separately(self):
        group_result = create_and_send_mailing(
            subject="Message réservé aux groupes",
            body_html="<p>Contenu pour les groupes.</p>",
            recipient_kinds=(MailingDelivery.RecipientKind.TEACHER,),
        )

        self.assertEqual(group_result.sent_count, 2)
        self.assertEqual(
            group_result.campaign.audience, MailingCampaign.Audience.GROUPS
        )
        self.assertFalse(
            group_result.campaign.deliveries.exclude(
                recipient_kind=MailingDelivery.RecipientKind.TEACHER
            ).exists()
        )
        self.assertTrue(
            all(message.subject == "Message réservé aux groupes" for message in mail.outbox)
        )

        mail.outbox.clear()
        organizer_result = create_and_send_mailing(
            organizer_subject="Message réservé aux animations",
            organizer_body_html="<p>Contenu pour les animations.</p>",
            recipient_kinds=(MailingDelivery.RecipientKind.ORGANIZER,),
        )

        self.assertEqual(organizer_result.sent_count, 1)
        self.assertEqual(
            organizer_result.campaign.audience,
            MailingCampaign.Audience.ORGANIZERS,
        )
        self.assertFalse(
            organizer_result.campaign.deliveries.exclude(
                recipient_kind=MailingDelivery.RecipientKind.ORGANIZER
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(
            all(message.subject == "Message réservé aux animations" for message in mail.outbox)
        )
        self.assertTrue(
            all("Contenu pour les animations" in message.body for message in mail.outbox)
        )

    def test_template_variables_escape_html_and_use_frozen_snapshot(self):
        teacher = self.first_registration.teacher
        teacher.first_name = "<Marie>"
        teacher.last_name = "Du & Pont"
        teacher.save(update_fields=("first_name", "last_name"))
        campaign = create_mailing_campaign(
            subject="Informations",
            body_html=(
                "<p>{{ prenom }} {{ nom }} — {{ programme }} — "
                "{{ nombre_inscrits }}</p>"
            ),
            visit_date=date(2026, 9, 23),
        )

        teacher.first_name = "Prénom modifié"
        teacher.last_name = "Nom modifié"
        teacher.save(update_fields=("first_name", "last_name"))
        Animation.objects.filter(pk=self.first_session.animation_id).update(
            title="Animation modifiée"
        )

        send_mailing_campaign(campaign)

        message = next(
            message for message in mail.outbox if message.to == ["prof-a@example.test"]
        )
        html_body = message.alternatives[0].content
        self.assertIn("&lt;Marie&gt; Du &amp; Pont", html_body)
        self.assertIn("Le sol vivant", html_body)
        self.assertIn("26", html_body)
        self.assertNotIn("Prénom modifié", html_body)
        self.assertNotIn("Animation modifiée", html_body)

    def test_unknown_template_variable_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "Variable de publipostage inconnue"):
            create_mailing_campaign(
                subject="Informations",
                body_html="<p>Bonjour {{ adresse_email }}</p>",
            )

    @patch("communication.mailing.EmailMultiAlternatives.send")
    def test_failures_are_logged_and_only_explicitly_retried(self, mocked_send):
        mocked_send.side_effect = RuntimeError(
            "Échec SMTP pour responsable@example.test\nsecret"
        )
        campaign = create_mailing_campaign(
            subject="Informations",
            body_html="<p>Texte.</p>",
        )

        first = send_mailing_campaign(campaign)
        self.assertEqual(first.failed_count, 3)
        self.assertEqual(mocked_send.call_count, 3)
        failed = campaign.deliveries.get(recipient__iexact="responsable@example.test")
        self.assertNotIn("responsable@example.test", failed.error_summary)
        self.assertNotIn("\n", failed.error_summary)
        self.assertEqual(failed.attempts, 1)

        send_mailing_campaign(campaign)
        self.assertEqual(mocked_send.call_count, 3)

        mocked_send.side_effect = None
        mocked_send.return_value = 1
        retried = send_mailing_campaign(campaign, retry_failed=True)
        self.assertEqual(retried.sent_count, 3)
        self.assertEqual(mocked_send.call_count, 6)
        self.assertFalse(
            campaign.deliveries.exclude(
                status=MailingDelivery.Status.SENT, attempts=2
            ).exists()
        )

    def test_same_idempotency_key_rejects_different_content(self):
        create_mailing_campaign(
            subject="Informations",
            body_html="<p>Première version.</p>",
            idempotency_key="stable-key",
        )

        with self.assertRaisesMessage(ValueError, "autre envoi"):
            create_mailing_campaign(
                subject="Informations",
                body_html="<p>Autre version.</p>",
                idempotency_key="stable-key",
            )

    def test_stale_sending_delivery_requires_an_explicit_retry(self):
        campaign = create_mailing_campaign(
            subject="Informations",
            body_html="<p>Texte.</p>",
        )
        interrupted = campaign.deliveries.order_by("pk").first()
        MailingDelivery.objects.filter(pk=interrupted.pk).update(
            status=MailingDelivery.Status.SENDING,
            last_attempted_at=timezone.now() - timedelta(minutes=10),
        )

        first = send_mailing_campaign(campaign)

        interrupted.refresh_from_db()
        self.assertEqual(interrupted.status, MailingDelivery.Status.FAILED)
        self.assertEqual(first.sent_count, 2)
        self.assertEqual(first.failed_count, 1)
        self.assertEqual(len(mail.outbox), 2)

        second = send_mailing_campaign(campaign, retry_failed=True)

        self.assertEqual(second.sent_count, 3)
        self.assertEqual(second.failed_count, 0)
        self.assertEqual(len(mail.outbox), 3)
