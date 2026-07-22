from datetime import date, time
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from catalogue.models import Animation, Category, SchoolLevel, Session
from inscriptions.codes import generate_unique_group_code
from inscriptions.models import (
    GroupFamily,
    Institution,
    Registration,
    Reservation,
    Teacher,
)


class GroupModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.level = SchoolLevel.objects.create(code="C3", label="Cycle 3")
        cls.institution = Institution.objects.create(
            name="École du Verger",
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

    def make_registration(self, suffix, **overrides):
        values = {
            "institution": self.institution,
            "teacher": self.teacher,
            "group_name": f"Groupe {suffix}",
            "school_level": self.level,
            "student_count": 24,
            "chaperone_count": 2,
            "visit_date": date(2026, 9, 23),
            "status": Registration.Status.CONFIRMED,
            "edit_token_digest": suffix.zfill(64),
        }
        values.update(overrides)
        return Registration.objects.create(**values)

    def test_institution_address_and_postal_code_are_optional(self):
        self.institution.full_clean()

        self.assertEqual(self.institution.address, "")
        self.assertEqual(self.institution.postal_code, "")

    def test_registration_generates_and_normalizes_readable_unique_codes(self):
        with patch(
            "inscriptions.codes.generate_unique_group_code",
            return_value="pomme-vif",
        ):
            generated = self.make_registration("1")
        supplied = self.make_registration("2", group_code="Truffe Dorée")

        self.assertRegex(generated.group_code, r"^[a-z]+-[a-z]+$")
        self.assertEqual(supplied.group_code, "truffe-doree")
        self.assertNotEqual(generated.group_code, supplied.group_code)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_registration("3", group_code="Truffe Dorée")

    def test_unique_code_suggestion_skips_an_existing_combination(self):
        self.make_registration("4", group_code="truffe-dore")

        with patch(
            "inscriptions.codes.generate_group_code_candidate",
            side_effect=("truffe-dore", "pomme-vif"),
        ):
            suggestion = generate_unique_group_code()

        self.assertEqual(suggestion, "pomme-vif")

    def test_family_is_configurable_protected_and_totals_are_calculated(self):
        family = GroupFamily.objects.create(
            name="Enseignement agricole",
            slug="enseignement-agricole",
        )
        registration = self.make_registration(
            "5",
            family=family,
            level_comment="Classe multiniveau",
        )
        category = Category.objects.create(name="Nature", slug="nature")
        animation = Animation.objects.create(
            title="Le sol vivant",
            slug="sol-vivant",
            short_description="Découvrir le sol.",
            category=category,
            indicative_duration=45,
        )
        session = Session.objects.create(
            animation=animation,
            date=registration.visit_date,
            starts_at=time(10),
            ends_at=time(10, 45),
            location="Pôle sols",
            max_capacity=30,
        )
        reservation = Reservation.objects.create(
            registration=registration,
            session=session,
            student_count=18,
            chaperone_count=2,
        )

        self.assertEqual(str(family), "Enseignement agricole")
        self.assertEqual(registration.total_participant_count, 26)
        self.assertEqual(reservation.total_participant_count, 20)
        self.assertEqual(registration.level_comment, "Classe multiniveau")
        with self.assertRaises(ProtectedError):
            family.delete()
