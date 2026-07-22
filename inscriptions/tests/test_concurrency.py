import queue
import threading
from datetime import date, datetime, time
from unittest import skipUnless

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings

from catalogue.models import Animation, Category, SchoolLevel, Session
from inscriptions.models import Institution, Teacher
from inscriptions.services.capacity import CapacityExceeded
from inscriptions.services.registration import ReservationRequest, create_draft


@skipUnless(connection.vendor == "postgresql", "Le verrouillage concurrent exige PostgreSQL.")
@override_settings(
    EVENT_DATES=("2026-09-23", "2026-09-24"),
    REGISTRATION_EDIT_DEADLINE=datetime.fromisoformat("2026-09-16T23:59:00+02:00"),
)
class ConcurrentCapacityTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.level = SchoolLevel.objects.create(code="CM2", label="CM2")
        category = Category.objects.create(name="Agriculture", slug="agriculture")
        animation = Animation.objects.create(
            title="Sols",
            slug="sols",
            short_description="Atelier",
            category=category,
            indicative_duration=60,
        )
        self.session = Session.objects.create(
            animation=animation,
            date=date(2026, 9, 23),
            starts_at=time(9),
            ends_at=time(10),
            location="Hall A",
            max_capacity=10,
        )
        self.institution = Institution.objects.create(
            name="École",
            address="1 rue du Test",
            postal_code="35000",
            city="Rennes",
            department="35",
        )
        self.teacher = Teacher.objects.create(
            institution=self.institution,
            first_name="Alice",
            last_name="Martin",
            email="alice@example.test",
            phone="0102030405",
        )

    def test_two_simultaneous_writes_cannot_overbook(self):
        barrier = threading.Barrier(2)
        results = queue.Queue()
        now = datetime.fromisoformat("2026-09-01T10:00:00+02:00")

        def reserve(group_name):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                create_draft(
                    institution=Institution.objects.get(pk=self.institution.pk),
                    teacher=Teacher.objects.get(pk=self.teacher.pk),
                    group_name=group_name,
                    school_level=SchoolLevel.objects.get(pk=self.level.pk),
                    student_count=6,
                    chaperone_count=1,
                    visit_date=date(2026, 9, 23),
                    reservation_requests=[
                        ReservationRequest(
                            self.session.pk,
                            student_count=6,
                            chaperone_count=1,
                        )
                    ],
                    at=now,
                )
            except CapacityExceeded:
                results.put("full")
            else:
                results.put("created")
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=reserve, args=(f"Groupe {index}",))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual([results.get_nowait(), results.get_nowait()], ["created", "full"])
