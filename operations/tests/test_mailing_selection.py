from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from communication.models import MailingCampaign, MailingDelivery
from inscriptions.models import Registration

from .factories import create_operational_data


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class MailingSelectionViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = create_operational_data()
        session = cls.data["session"]
        session.organizer = "Alice Responsable"
        session.organizer_email = "alice@example.test"
        session.save(update_fields=("organizer", "organizer_email"))
        cls.staff = get_user_model().objects.create_superuser(
            username="mail-staff", password="secret", email="staff@example.test"
        )

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse("operations:mailing-create")
        response = self.client.get(self.url)
        fields = response.context["form"].fields
        self.teacher_key = fields["teacher_recipients"].choices[0][0]
        self.organizer_key = fields["organizer_recipients"].choices[0][0]

    def payload(self, **overrides):
        return {
            "recipient_mode": "selected",
            "subject": "Message au groupe",
            "body_html": "<p>Bonjour {{ prenom }}.</p>",
            "organizer_subject": "Message au lieu",
            "organizer_body_html": "<p>Voici les groupes attendus.</p>",
            "action": "send_both",
            **overrides,
        }

    def test_lists_display_existing_contacts_and_no_preselected_recipients(self):
        response = self.client.get(self.url)
        for label in ("Choisir les responsables", "Marie Dupont", "marie@example.test",
                      "Alice Responsable", "Pôle sols", "Tout décocher"):
            self.assertContains(response, label)
        form = response.context["form"]
        self.assertEqual(form["recipient_mode"].value(), "all")
        self.assertFalse(form["teacher_recipients"].value())
        self.assertFalse(form["organizer_recipients"].value())
        for instruction in ("Imprimez ce courriel", "15 minutes", "Louise Le Moing"):
            self.assertIn(instruction, response.context["editor_html"])
            self.assertNotIn(instruction, response.context["organizer_editor_html"])

    def test_each_send_button_respects_manual_selection_and_its_public(self):
        cases = (
            ("send_groups", "teacher_recipients", self.teacher_key, "marie@example.test"),
            ("send_organizers", "organizer_recipients", self.organizer_key, "alice@example.test"),
        )
        for action, field, key, expected in cases:
            with self.subTest(action=action):
                mail.outbox.clear()
                response = self.client.post(
                    self.url, self.payload(**{field: [key], "action": action})
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual([message.to for message in mail.outbox], [[expected]])
                self.assertEqual(mail.outbox[0].cc, [self.staff.email])
                campaign = MailingCampaign.objects.latest("pk")
                self.assertEqual(campaign.recipient_selection, [key])
                detail = self.client.get(response.url)
                self.assertContains(detail, "sélectionnés manuellement")

    def test_both_publics_can_send_to_a_subset_without_broadening_it(self):
        response = self.client.post(
            self.url, self.payload(organizer_recipients=[self.organizer_key])
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual([message.to for message in mail.outbox], [["alice@example.test"]])
        self.assertEqual(MailingDelivery.objects.count(), 1)

    def test_group_send_does_not_contact_checked_organizers(self):
        response = self.client.post(
            self.url,
            self.payload(
                action="send_groups",
                teacher_recipients=[self.teacher_key],
                organizer_recipients=[self.organizer_key],
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual([message.to for message in mail.outbox], [["marie@example.test"]])
        self.assertEqual(MailingCampaign.objects.get().recipient_selection, [self.teacher_key])

    def test_empty_or_wrong_public_selection_does_not_send(self):
        for action in ("send_groups", "send_organizers", "send_both"):
            with self.subTest(action=action):
                response = self.client.post(self.url, self.payload(action=action))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Sélectionnez au moins un responsable")
        response = self.client.post(
            self.url, self.payload(action="send_groups", organizer_recipients=[self.organizer_key])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sélectionnez au moins un responsable")
        self.assertFalse(MailingCampaign.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_unlisted_and_now_excluded_contacts_are_rejected(self):
        for payload in (
            self.payload(teacher_recipients=["unknown"]),
            self.payload(teacher_recipients=[self.teacher_key], visit_date="2026-09-24"),
        ):
            with self.subTest(payload=payload):
                response = self.client.post(self.url, payload)
                self.assertEqual(response.status_code, 200)
                self.assertIn("teacher_recipients", response.context["form"].errors)
        self.assertFalse(MailingCampaign.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_no_longer_confirmed_group_cannot_be_selected(self):
        Registration.objects.filter(pk=self.data["registration"].pk).update(status="DRAFT")
        response = self.client.post(
            self.url, self.payload(teacher_recipients=[self.teacher_key])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("teacher_recipients", response.context["form"].errors)
        self.assertEqual(len(mail.outbox), 0)

    def test_preview_preserves_selection_and_does_not_send(self):
        response = self.client.post(
            self.url,
            self.payload(action="preview", teacher_recipients=[self.teacher_key]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["preview"].teacher_count, 1)
        self.assertEqual(response.context["preview"].organizer_count, 0)
        self.assertEqual(response.context["form"]["teacher_recipients"].value(), [self.teacher_key])
        self.assertContains(response, " checked")
        self.assertFalse(MailingCampaign.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_selected_contacts_do_not_default_to_everyone_if_mode_is_missing(self):
        payload = self.payload(teacher_recipients=[self.teacher_key])
        del payload["recipient_mode"]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual([message.to for message in mail.outbox], [["marie@example.test"]])

    def test_manual_sending_requires_the_mailing_permission(self):
        staff = get_user_model().objects.create_user(username="read-only", is_staff=True)
        self.client.force_login(staff)
        response = self.client.post(
            self.url, self.payload(teacher_recipients=[self.teacher_key])
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MailingCampaign.objects.exists())
        self.assertEqual(len(mail.outbox), 0)
