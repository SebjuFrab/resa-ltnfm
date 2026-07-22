from datetime import date, time
from unittest.mock import patch

from django.contrib.admin import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from catalogue.admin import AnimationAdmin, SessionAdmin, SessionAdminForm
from catalogue.models import Animation, Category, SchoolLevel, Session
from inscriptions.models import Institution, Registration, Reservation, Teacher


class CatalogueAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name="Agriculture", slug="agriculture")
        cls.level = SchoolLevel.objects.create(code="LYC", label="Lycée", sort_order=50)
        cls.animation = Animation.objects.create(
            title="Cultiver demain",
            slug="cultiver-demain",
            short_description="Découvrir des pratiques agricoles durables.",
            description="Description complète",
            category=cls.category,
            indicative_duration=60,
            instructions="Prévoir des chaussures fermées.",
            accessibility="Accessible aux personnes à mobilité réduite.",
        )
        cls.animation.recommended_levels.add(cls.level)

    def setUp(self):
        self.request = RequestFactory().post("/admin/")
        self.request.user = get_user_model().objects.create_superuser(
            username=f"admin-{self._testMethodName}",
            email="admin@example.test",
            password="secret",
        )
        self.site = AdminSite()

    def test_duplicate_animation_copies_content_and_levels_as_inactive(self):
        model_admin = AnimationAdmin(Animation, self.site)

        with patch.object(model_admin, "message_user"):
            model_admin.duplicate_animations(
                self.request,
                Animation.objects.filter(pk=self.animation.pk),
            )

        copy = Animation.objects.exclude(pk=self.animation.pk).get()
        self.assertEqual(copy.title, "Copie de Cultiver demain")
        self.assertEqual(copy.slug, "cultiver-demain-copie")
        self.assertEqual(copy.category, self.category)
        self.assertEqual(copy.indicative_duration, 60)
        self.assertEqual(list(copy.recommended_levels.all()), [self.level])
        self.assertFalse(copy.is_active)

    def test_duplicate_animation_generates_a_unique_slug(self):
        model_admin = AnimationAdmin(Animation, self.site)

        with patch.object(model_admin, "message_user"):
            for _ in range(2):
                model_admin.duplicate_animations(
                    self.request,
                    Animation.objects.filter(pk=self.animation.pk),
                )

        self.assertEqual(
            set(Animation.objects.exclude(pk=self.animation.pk).values_list("slug", flat=True)),
            {"cultiver-demain-copie", "cultiver-demain-copie-2"},
        )

    def test_session_actions_change_status(self):
        session = Session.objects.create(
            animation=self.animation,
            date=date(2026, 9, 23),
            starts_at=time(10),
            ends_at=time(11),
            location="Chapiteau",
            max_capacity=25,
        )
        model_admin = SessionAdmin(Session, self.site)

        with patch.object(model_admin, "message_user"):
            model_admin.close_sessions(
                self.request,
                Session.objects.filter(pk=session.pk),
            )
        session.refresh_from_db()
        self.assertEqual(session.status, Session.Status.CLOSED)

        with patch.object(model_admin, "message_user"):
            model_admin.cancel_sessions(
                self.request,
                Session.objects.filter(pk=session.pk),
            )
        session.refresh_from_db()
        self.assertEqual(session.status, Session.Status.CANCELLED)

    def test_capacity_cannot_be_lowered_below_active_reservations(self):
        session = Session.objects.create(
            animation=self.animation,
            date=date(2026, 9, 23),
            starts_at=time(10),
            ends_at=time(11),
            location="Chapiteau",
            max_capacity=25,
        )
        institution = Institution.objects.create(
            name="Lycée test",
            address="1 rue Test",
            postal_code="35000",
            city="Rennes",
            department="35",
        )
        teacher = Teacher.objects.create(
            institution=institution,
            first_name="Marie",
            last_name="Test",
            email="marie@example.test",
            phone="0102030405",
        )
        registration = Registration.objects.create(
            institution=institution,
            teacher=teacher,
            group_name="Groupe test",
            school_level=self.level,
            student_count=10,
            visit_date=date(2026, 9, 23),
            status=Registration.Status.CONFIRMED,
            edit_token_digest="c" * 64,
        )
        Reservation.objects.create(
            registration=registration,
            session=session,
            student_count=10,
            chaperone_count=2,
        )

        form = SessionAdminForm(
            instance=session,
            data={
                "animation": self.animation.pk,
                "date": "2026-09-23",
                "starts_at": "10:00",
                "ends_at": "11:00",
                "location": "Chapiteau",
                "max_capacity": 11,
                "status": Session.Status.OPEN,
                "organizer": "",
                "internal_comment": "",
            },
        )

        self.assertFalse(form.is_valid())
        self.assertIn("max_capacity", form.errors)
