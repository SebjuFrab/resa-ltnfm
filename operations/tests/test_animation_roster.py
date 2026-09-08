from datetime import date, time, timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalogue.models import Animation, Session
from inscriptions.models import Registration, Reservation

from .factories import create_operational_data


class AnimationRosterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = create_operational_data()
        cls.staff = get_user_model().objects.create_superuser(
            username="roster-admin", email="admin@example.test", password="secret"
        )

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse("operations:animation-roster", args=[self.data["animation"].pk])

    def _reserve(self, name, *, status=Registration.Status.DRAFT, expires=None):
        source = self.data["registration"]
        registration = Registration.objects.create(
            group_name=name,
            group_code=name,
            institution=source.institution,
            teacher=source.teacher,
            school_level=source.school_level,
            student_count=24,
            chaperone_count=2,
            visit_date=source.visit_date,
            status=status,
            draft_expires_at=expires,
            edit_token_digest=uuid4().hex * 2,
        )
        return Reservation.objects.create(
            registration=registration,
            session=self.data["session"],
            student_count=7,
            chaperone_count=1,
        )

    def test_roster_shows_partial_allocations_and_excludes_released_places(self):
        reservation = self.data["reservation"]
        reservation.student_count = 12
        reservation.chaperone_count = 1
        reservation.save()
        self._reserve("brouillon-permanent")
        self._reserve("brouillon-temporaire", expires=timezone.now() + timedelta(hours=1))
        expired = self._reserve("expire", expires=timezone.now() - timedelta(hours=1))
        cancelled = self._reserve("annule", status=Registration.Status.CANCELLED)
        removed = self._reserve("retire", status=Registration.Status.CONFIRMED)
        removed.status = Reservation.Status.CANCELLED
        removed.save()
        second_session = Session.objects.create(
            animation=self.data["animation"], date=date(2026, 9, 24),
            starts_at=time(14), ends_at=time(15), location="Deuxième lieu", max_capacity=30,
        )
        Reservation.objects.create(
            registration=self.data["registration"], session=second_session,
            student_count=5, chaperone_count=1,
        )

        response = self.client.get(self.url)

        self.assertContains(response, "Lycée des Champs")
        self.assertContains(response, "Marie Dupont")
        self.assertContains(response, "mailto:marie@example.test")
        self.assertContains(response, "13 personnes")
        self.assertContains(response, "Brouillon — places réservées")
        self.assertContains(response, reverse(
            "operations:registration-detail", args=[self.data["registration"].reference]
        ))
        self.assertEqual(response.context["group_count"], 3)
        self.assertEqual(response.context["participant_count"], 35)
        rows = response.context["session_rows"]
        self.assertEqual([row["participant_count"] for row in rows], [29, 6])
        present_ids = {r.pk for row in rows for r in row["reservations"]}
        self.assertTrue(present_ids.isdisjoint({expired.pk, cancelled.pk, removed.pk}))

    def test_day_filter_and_animation_scope(self):
        other_animation = Animation.objects.create(
            title="Autre animation", slug="autre", indicative_duration=60,
        )
        Session.objects.create(
            animation=other_animation, date=date(2026, 9, 23),
            starts_at=time(12), ends_at=time(13), location="Lieu hors périmètre", max_capacity=30,
        )
        Session.objects.create(
            animation=self.data["animation"], date=date(2026, 9, 24),
            starts_at=time(14), ends_at=time(15), location="Deuxième jour", max_capacity=30,
        )
        cancelled_session = Session.objects.create(
            animation=self.data["animation"], date=date(2026, 9, 24),
            starts_at=time(15), ends_at=time(16), location="Séance annulée", max_capacity=30,
            status=Session.Status.CANCELLED,
        )
        Reservation.objects.create(
            registration=self.data["registration"], session=cancelled_session, student_count=4,
        )

        response = self.client.get(self.url, {"date": "2026-09-24"})

        self.assertContains(response, "Deuxième jour")
        self.assertContains(response, "Aucun groupe inscrit à ce créneau.")
        self.assertNotContains(response, "Séance annulée")
        self.assertNotContains(response, "Lieu hors périmètre")
        self.assertNotContains(response, "Marie Dupont")
        self.assertEqual(response.context["group_count"], 0)
        self.assertEqual(len(response.context["session_rows"]), 1)
        self.assertContains(response, "data-auto-submit-filters")
        invalid = self.client.get(self.url, {"date": "not-a-date"})
        self.assertFalse(invalid.context["filter_form"].is_valid())
        self.assertEqual(invalid.context["session_rows"], [])

    def test_navigation_and_permissions_protect_teacher_details(self):
        listing = self.client.get(reverse("operations:animation-list"))
        self.assertContains(listing, f"{self.url}#seance-{self.data['session'].pk}")
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        restricted_staff = get_user_model().objects.create_user(
            username="catalogue-only", is_staff=True,
        )
        restricted_staff.user_permissions.add(Permission.objects.get(
            content_type__app_label="catalogue", codename="view_session",
        ))
        self.client.force_login(restricted_staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertNotContains(self.client.get(reverse("operations:animation-list")), self.url)

    def test_unknown_animation_is_not_found(self):
        self.assertEqual(self.client.get(
            reverse("operations:animation-roster", args=[999999])
        ).status_code, 404)
