from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from catalogue.models import Animation, Category, SchoolLevel, Session
from inscriptions.models import Institution, Registration, Reservation, Teacher


class CatalogueModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name="Biodiversité", slug="biodiversite")
        cls.level = SchoolLevel.objects.create(code="C3", label="Cycle 3", sort_order=30)
        cls.animation = Animation.objects.create(
            title="La vie du sol",
            slug="vie-du-sol",
            short_description="Observer et comprendre la vie du sol.",
            category=cls.category,
            indicative_duration=45,
        )
        cls.animation.recommended_levels.add(cls.level)

    def make_session(self, **overrides):
        values = {
            "animation": self.animation,
            "date": date(2026, 9, 23),
            "starts_at": time(9),
            "ends_at": time(9, 45),
            "location": "Hall 1",
            "max_capacity": 30,
        }
        values.update(overrides)
        return Session(**values)

    def test_labels_and_default_ordering_are_stable(self):
        SchoolLevel.objects.create(code="C2", label="Cycle 2", sort_order=20)

        self.assertEqual(str(self.category), "Biodiversité")
        self.assertEqual(str(self.level), "Cycle 3")
        self.assertEqual(
            list(
                SchoolLevel.objects.filter(code__in=("C2", "C3")).values_list(
                    "code", flat=True
                )
            ),
            ["C2", "C3"],
        )

    def test_school_level_is_active_by_default(self):
        level = SchoolLevel.objects.create(code="ACTIVE", label="Niveau actif")

        self.assertTrue(level.is_active)

    def test_animation_duration_must_be_strictly_positive(self):
        self.animation.indicative_duration = 0

        with self.assertRaises(ValidationError):
            self.animation.full_clean()

    def test_session_end_must_be_after_start(self):
        session = self.make_session(ends_at=time(9))

        with self.assertRaises(ValidationError):
            session.full_clean()

    def test_session_capacity_must_be_strictly_positive(self):
        session = self.make_session(max_capacity=0)

        with self.assertRaises(ValidationError):
            session.full_clean()

    def test_empty_unsaved_session_has_all_its_capacity_available(self):
        session = self.make_session()

        self.assertEqual(session.reserved_capacity, 0)
        self.assertEqual(session.remaining_capacity, 30)
        self.assertTrue(session.is_bookable)

    def test_session_string_contains_animation_date_and_time(self):
        session = self.make_session()

        self.assertEqual(str(session), "La vie du sol — 23/09/2026 à 09:00")

    def test_referenced_category_and_animation_are_protected(self):
        session = self.make_session()
        session.save()

        with self.assertRaises(ProtectedError):
            self.category.delete()
        with self.assertRaises(ProtectedError):
            self.animation.delete()


class SessionCapacityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        category = Category.objects.create(name="Climat", slug="climat")
        cls.level = SchoolLevel.objects.create(code="C4", label="Cycle 4")
        animation = Animation.objects.create(
            title="Comprendre le climat",
            slug="comprendre-climat",
            short_description="Expérimenter pour comprendre le climat.",
            category=category,
            indicative_duration=45,
        )
        cls.session = Session.objects.create(
            animation=animation,
            date=date(2026, 9, 24),
            starts_at=time(14),
            ends_at=time(14, 45),
            location="Salle B",
            max_capacity=30,
        )
        cls.institution = Institution.objects.create(
            name="Collège des Horizons",
            institution_type=Institution.Type.MIDDLE_SCHOOL,
            address="1 rue des Écoles",
            postal_code="35000",
            city="Rennes",
            department="35",
        )
        cls.teacher = Teacher.objects.create(
            institution=cls.institution,
            first_name="Alice",
            last_name="Martin",
            email="alice@example.test",
            phone="0102030405",
        )

    @classmethod
    def make_registration(cls, suffix, *, status, draft_expires_at=None):
        return Registration.objects.create(
            institution=cls.institution,
            teacher=cls.teacher,
            group_name=f"Groupe {suffix}",
            school_level=cls.level,
            student_count=20,
            visit_date=cls.session.date,
            status=status,
            draft_expires_at=draft_expires_at,
            edit_token_digest=suffix.zfill(64),
        )

    def test_capacity_counts_confirmed_and_unexpired_drafts_only(self):
        at = timezone.make_aware(datetime(2026, 9, 1, 12))
        confirmed = self.make_registration("1", status=Registration.Status.CONFIRMED)
        live_draft = self.make_registration(
            "2",
            status=Registration.Status.DRAFT,
            draft_expires_at=at + timedelta(minutes=30),
        )
        expired_draft = self.make_registration(
            "3",
            status=Registration.Status.DRAFT,
            draft_expires_at=at - timedelta(seconds=1),
        )
        cancelled = self.make_registration("4", status=Registration.Status.CANCELLED)
        cancelled_reservation = self.make_registration("5", status=Registration.Status.CONFIRMED)
        Reservation.objects.bulk_create(
            [
                Reservation(
                    registration=confirmed,
                    session=self.session,
                    student_count=8,
                    chaperone_count=2,
                ),
                Reservation(
                    registration=live_draft,
                    session=self.session,
                    student_count=5,
                    chaperone_count=1,
                ),
                Reservation(
                    registration=expired_draft,
                    session=self.session,
                    student_count=7,
                ),
                Reservation(
                    registration=cancelled,
                    session=self.session,
                    student_count=9,
                ),
                Reservation(
                    registration=cancelled_reservation,
                    session=self.session,
                    student_count=6,
                    status=Reservation.Status.CANCELLED,
                    cancelled_at=at,
                ),
            ]
        )

        with self.assertNumQueries(1):
            session = Session.objects.with_capacities(at=at).get(pk=self.session.pk)

        self.assertEqual(session.reserved_capacity, 16)
        self.assertEqual(session.remaining_capacity, 14)

    def test_capacity_properties_can_calculate_on_an_unannotated_instance(self):
        confirmed = self.make_registration("6", status=Registration.Status.CONFIRMED)
        Reservation.objects.create(
            registration=confirmed,
            session=self.session,
            student_count=12,
            chaperone_count=3,
        )
        session = Session.objects.get(pk=self.session.pk)

        self.assertEqual(session.reserved_capacity, 15)
        self.assertEqual(session.remaining_capacity, 15)
