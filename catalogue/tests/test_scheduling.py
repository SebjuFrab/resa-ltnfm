from datetime import date, time
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from catalogue.admin import AnimationAdmin
from catalogue.models import Animation, Session
from catalogue.scheduling import recalculate_session_end_times
from operations.tests.factories import create_operational_data


class AnimationDurationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = create_operational_data()
        cls.user = get_user_model().objects.create_superuser(
            username="duration-admin", email="admin@example.test", password="secret",
        )

    def make_session(self, **overrides):
        values = {
            "animation": self.data["animation"],
            "date": date(2026, 9, 24),
            "starts_at": time(14, 15),
            "ends_at": time(15),
            "location": "Pôle sols",
            "max_capacity": 30,
        }
        values.update(overrides)
        return Session.objects.create(**values)

    def bulk_request(self, **data):
        request = RequestFactory().post("/admin/catalogue/animation/", data=data)
        request.user = self.user
        return request

    def test_duration_change_recalculates_existing_slots_and_preserves_bookings(self):
        second = self.make_session()
        overlapping = self.make_session(starts_at=time(14, 30), ends_at=time(15, 15))
        animation = self.data["animation"]
        animation.indicative_duration = 90
        animation.save(update_fields=("indicative_duration", "updated_at"))

        first = self.data["session"]
        first.refresh_from_db()
        second.refresh_from_db()
        overlapping.refresh_from_db()
        self.assertEqual((first.starts_at, first.ends_at), (time(10), time(11, 30)))
        self.assertEqual((second.starts_at, second.ends_at), (time(14, 15), time(15, 45)))
        self.assertEqual(overlapping.ends_at, time(16))
        self.data["reservation"].refresh_from_db()
        self.assertEqual(self.data["reservation"].session_id, first.pk)
        self.assertEqual(self.data["reservation"].total_participant_count, 26)
        self.assertEqual(first.reserved_capacity, 26)
        self.assertEqual(len(mail.outbox), 0)

    def test_unrelated_change_does_not_overwrite_a_custom_end_time(self):
        session = self.make_session(ends_at=time(15, 30))
        animation = self.data["animation"]
        animation.title = "Nouveau titre"
        animation.save()
        session.refresh_from_db()
        self.assertEqual(session.ends_at, time(15, 30))
        animation.indicative_duration = 120
        animation.save(update_fields=("title",))
        session.refresh_from_db()
        self.assertEqual(session.ends_at, time(15, 30))

    def test_midnight_end_is_validated_and_duration_and_slots_are_rolled_back(self):
        self.make_session(starts_at=time(23, 30), ends_at=time(23, 50))
        animation = self.data["animation"]
        animation.indicative_duration = 60
        with self.assertRaises(ValidationError) as caught:
            animation.full_clean()
        self.assertIn("indicative_duration", caught.exception.message_dict)
        with self.assertRaisesMessage(ValidationError, "lendemain"):
            animation.save()
        animation.refresh_from_db()
        self.data["session"].refresh_from_db()
        self.assertEqual(animation.indicative_duration, 45)
        self.assertEqual(self.data["session"].ends_at, time(10, 45))

    def test_exact_duplicate_is_reported_without_merging_sessions(self):
        second = self.make_session(
            date=date(2026, 9, 23), starts_at=time(10), ends_at=time(11), location="PÔLE SOLS",
        )
        animation = self.data["animation"]
        animation.indicative_duration = 90
        with self.assertRaisesMessage(ValidationError, "exactement les mêmes horaires"):
            animation.save()
        self.data["session"].refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.data["session"].ends_at, time(10, 45))
        self.assertEqual(second.ends_at, time(11))
        self.assertEqual(animation.sessions.count(), 2)

    def test_recalculation_action_repairs_previously_entered_durations(self):
        animation = self.data["animation"]
        # Simulate data saved before automatic synchronization existed.
        Animation.objects.filter(pk=animation.pk).update(indicative_duration=75)
        model_admin = AnimationAdmin(Animation, admin.site)
        with patch.object(model_admin, "message_user"):
            model_admin.recalculate_schedules(
                self.bulk_request(), Animation.objects.filter(pk=animation.pk),
            )
        session = self.data["session"]
        session.refresh_from_db()
        updated_at = session.updated_at
        self.assertEqual(session.ends_at, time(11, 15))
        self.assertEqual(recalculate_session_end_times(animation), 0)
        session.refresh_from_db()
        self.assertEqual(session.updated_at, updated_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_bulk_duration_recalculates_even_when_duration_is_unchanged(self):
        session = self.make_session(ends_at=time(16))
        model_admin = AnimationAdmin(Animation, admin.site)
        with patch.object(model_admin, "message_user"):
            response = model_admin.bulk_update_animations(
                self.bulk_request(
                    apply_bulk_update="1", apply_indicative_duration="on", indicative_duration="45",
                ),
                Animation.objects.filter(pk=self.data["animation"].pk),
            )
        self.assertIsNone(response)
        session.refresh_from_db()
        self.assertEqual(session.ends_at, time(15))

    def test_bulk_invalid_duration_returns_form_error_and_rolls_back_all_animations(self):
        late_animation = Animation.objects.create(
            title="Animation tardive", slug="animation-tardive", indicative_duration=15,
        )
        self.make_session(
            animation=late_animation, starts_at=time(23, 30), ends_at=time(23, 45),
        )
        response = AnimationAdmin(Animation, admin.site).bulk_update_animations(
            self.bulk_request(
                apply_bulk_update="1", apply_indicative_duration="on", indicative_duration="120",
            ),
            Animation.objects.filter(pk__in=[self.data["animation"].pk, late_animation.pk]),
        )
        self.assertIn("lendemain", str(response.context_data["form"].non_field_errors()))
        self.data["animation"].refresh_from_db()
        self.data["session"].refresh_from_db()
        self.assertEqual(self.data["animation"].indicative_duration, 45)
        self.assertEqual(self.data["session"].ends_at, time(10, 45))

    def test_admin_change_form_recalculates_readonly_inline_sessions(self):
        animation = self.data["animation"]
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("admin:catalogue_animation_change", args=[animation.pk]),
            {
                "title": animation.title, "slug": animation.slug,
                "short_description": animation.short_description,
                "venue_category": animation.venue_category,
                "themes": list(animation.themes.values_list("pk", flat=True)),
                "indicative_duration": "75", "is_active": "on",
                "sessions-TOTAL_FORMS": "1", "sessions-INITIAL_FORMS": "1",
                "sessions-MIN_NUM_FORMS": "0", "sessions-MAX_NUM_FORMS": "0",
                "sessions-0-id": str(self.data["session"].pk),
                "sessions-0-animation": str(animation.pk),
                "_save": "Enregistrer",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.data["session"].refresh_from_db()
        self.assertEqual(self.data["session"].ends_at, time(11, 15))
