from datetime import date, time

from django.utils import timezone

from catalogue.models import Animation, Category, SchoolLevel, Session
from inscriptions.models import Institution, Registration, Reservation, Teacher


def create_operational_data(*, institution_name="Lycée des Champs"):
    category = Category.objects.create(name="Nature", slug="nature")
    level = SchoolLevel.objects.create(code="LYCEE", label="Lycée")
    animation = Animation.objects.create(
        title="Le sol vivant",
        slug="sol-vivant",
        short_description="Découvrir le sol.",
        category=category,
        indicative_duration=45,
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
        name=institution_name,
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
    registration = Registration.objects.create(
        institution=institution,
        teacher=teacher,
        group_name="Seconde A",
        school_level=level,
        student_count=24,
        chaperone_count=2,
        visit_date=date(2026, 9, 23),
        status=Registration.Status.CONFIRMED,
        edit_token_digest="b" * 64,
        confirmed_at=timezone.now(),
    )
    reservation = Reservation.objects.create(
        registration=registration,
        session=session,
        student_count=24,
        chaperone_count=2,
    )
    return {
        "animation": animation,
        "session": session,
        "registration": registration,
        "reservation": reservation,
    }

