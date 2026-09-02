from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

from catalogue.models import Animation, Category, SchoolLevel, Session
from inscriptions.models import (
    GroupFamily,
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
    Teacher,
)
from inscriptions.services.capacity import (
    SessionUnavailable,
    capacity_warnings,
    reserved_participant_count,
    reserved_student_count,
)
from inscriptions.services.registration import (
    InvalidProgram,
    InvalidRegistrationData,
    RegistrationNotEditable,
    ReservationRequest,
    cancel_registration,
    confirm_registration,
    create_draft,
    update_registration,
)
from inscriptions.services.tokens import (
    InvalidEditToken,
    get_registration_for_token,
    rotate_registration_token,
)


@override_settings(
    EVENT_DATES=("2026-09-23", "2026-09-24"),
    REGISTRATION_EDIT_DEADLINE=datetime.fromisoformat("2026-09-16T23:59:00+02:00"),
    DRAFT_HOLD_MINUTES=60,
)
class RegistrationServiceTests(TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat("2026-09-01T10:00:00+02:00")
        self.level = SchoolLevel.objects.create(code="CM2", label="CM2")
        category = Category.objects.create(name="Agriculture", slug="agriculture")
        self.animation = Animation.objects.create(
            title="Découvrir les sols",
            slug="sols",
            short_description="Atelier autour du sol",
            category=category,
            indicative_duration=60,
        )
        self.institution = Institution.objects.create(
            name="École des Tilleuls",
            institution_type=Institution.Type.PRIMARY_SCHOOL,
            address="1 rue des Écoles",
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

    def session(self, starts_at, ends_at, *, capacity=30, status=Session.Status.OPEN, day=23):
        return Session.objects.create(
            animation=self.animation,
            date=date(2026, 9, day),
            starts_at=starts_at,
            ends_at=ends_at,
            location="Hall A",
            max_capacity=capacity,
            status=status,
        )

    def draft(self, requests, *, student_count=20, chaperone_count=2, at=None):
        return create_draft(
            institution=self.institution,
            teacher=self.teacher,
            group_name="CM2 A",
            school_level=self.level,
            student_count=student_count,
            chaperone_count=chaperone_count,
            visit_date=date(2026, 9, 23),
            reservation_requests=requests,
            at=at or self.now,
        )

    def test_capacity_counts_all_participants_and_draft_expires(self):
        session = self.session(time(9), time(10), capacity=10)
        self.draft(
            [ReservationRequest(session.pk, student_count=8, chaperone_count=2)],
            student_count=8,
            chaperone_count=2,
        )

        self.assertEqual(
            reserved_participant_count(session, at=self.now + timedelta(minutes=59)),
            10,
        )
        self.assertEqual(
            reserved_participant_count(session, at=self.now + timedelta(minutes=61)),
            0,
        )
        self.assertEqual(
            reserved_student_count(session, at=self.now + timedelta(minutes=59)),
            10,
        )

    def test_chaperone_increase_can_exceed_capacity(self):
        session = self.session(time(9), time(10), capacity=10)
        first = self.draft(
            [ReservationRequest(session.pk, 5, chaperone_count=1)],
            student_count=5,
            chaperone_count=2,
        )
        self.draft(
            [ReservationRequest(session.pk, 3, chaperone_count=0)],
            student_count=3,
            chaperone_count=0,
        )

        update_registration(
            first.registration,
            reservation_requests=[ReservationRequest(session.pk, 5, chaperone_count=3)],
            chaperone_count=3,
            at=self.now + timedelta(minutes=5),
        )

        reservation = first.registration.reservations.get(status=Reservation.Status.ACTIVE)
        self.assertEqual(reservation.chaperone_count, 3)
        self.assertEqual(reserved_participant_count(session, at=self.now), 11)

    def test_overlapping_sessions_check_chaperones_separately(self):
        first = self.session(time(9), time(10), capacity=30)
        second = self.session(time(9, 30), time(10, 30), capacity=30)

        with self.assertRaisesMessage(InvalidProgram, "accompagnateur"):
            self.draft(
                [
                    ReservationRequest(first.pk, 5, chaperone_count=1),
                    ReservationRequest(second.pk, 5, chaperone_count=1),
                ],
                student_count=10,
                chaperone_count=1,
            )

    def test_capacity_overrun_is_allowed_and_reported_as_a_warning(self):
        session = self.session(time(9), time(10), capacity=10)
        self.draft([ReservationRequest(session.pk, 6)], student_count=6)

        warnings = capacity_warnings({session.pk: 5}, at=self.now)
        second = self.draft([ReservationRequest(session.pk, 5)], student_count=5)

        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].projected_total, 11)
        self.assertEqual(warnings[0].excess, 1)
        self.assertIsNotNone(second.registration.pk)
        self.assertEqual(Registration.objects.count(), 2)
        self.assertEqual(reserved_student_count(session, at=self.now), 11)

    def test_reservation_can_be_modified_beyond_capacity(self):
        session = self.session(time(9), time(10), capacity=10)
        first = self.draft([ReservationRequest(session.pk, 6)], student_count=10)
        self.draft([ReservationRequest(session.pk, 4)], student_count=4)

        update_registration(
            first.registration,
            reservation_requests=[ReservationRequest(session.pk, 7)],
            at=self.now + timedelta(minutes=5),
        )

        reservation = first.registration.reservations.get(status=Reservation.Status.ACTIVE)
        self.assertEqual(reservation.student_count, 7)
        self.assertEqual(reserved_student_count(session, at=self.now), 11)

    def test_cancel_releases_capacity_and_revokes_token(self):
        session = self.session(time(9), time(10), capacity=10)
        access = self.draft([ReservationRequest(session.pk, 10)], student_count=10)

        cancelled = cancel_registration(access.registration, at=self.now + timedelta(minutes=5))

        self.assertEqual(cancelled.status, Registration.Status.CANCELLED)
        self.assertIsNotNone(cancelled.token_revoked_at)
        self.assertEqual(reserved_student_count(session, at=self.now + timedelta(minutes=6)), 0)
        self.assertFalse(cancelled.reservations.filter(status=Reservation.Status.ACTIVE).exists())
        with self.assertRaises(InvalidEditToken):
            get_registration_for_token(
                reference=cancelled.reference,
                token=access.edit_token,
                at=self.now + timedelta(minutes=6),
            )

    def test_partial_overlaps_use_the_group_total(self):
        first = self.session(time(9), time(10), capacity=20)
        second = self.session(time(9, 30), time(10, 30), capacity=20)

        with self.assertRaises(InvalidProgram):
            self.draft(
                [ReservationRequest(first.pk, 6), ReservationRequest(second.pk, 5)],
                student_count=10,
            )

    def test_adjacent_sessions_and_gaps_are_allowed(self):
        first = self.session(time(9), time(10), capacity=20)
        adjacent = self.session(time(10), time(11), capacity=20)
        after_gap = self.session(time(12), time(13), capacity=20)

        access = self.draft(
            [
                ReservationRequest(first.pk, 10),
                ReservationRequest(adjacent.pk, 10),
                ReservationRequest(after_gap.pk, 10),
            ],
            student_count=10,
        )

        self.assertEqual(access.registration.reservations.count(), 3)

    def test_lowering_group_size_rechecks_existing_overlaps(self):
        first = self.session(time(9), time(10), capacity=20)
        second = self.session(time(9, 30), time(10, 30), capacity=20)
        access = self.draft(
            [ReservationRequest(first.pk, 7), ReservationRequest(second.pk, 6)],
            student_count=15,
        )

        with self.assertRaises(InvalidProgram):
            update_registration(access.registration, student_count=12, at=self.now)

        access.registration.refresh_from_db()
        self.assertEqual(access.registration.student_count, 15)

    def test_closed_session_is_rejected(self):
        session = self.session(time(9), time(10), status=Session.Status.CLOSED)

        with self.assertRaises(SessionUnavailable):
            self.draft([ReservationRequest(session.pk, 5)], student_count=5)

    def test_unchanged_closed_reservation_does_not_block_contact_update(self):
        session = self.session(time(9), time(10), capacity=10)
        access = self.draft([ReservationRequest(session.pk, 5)], student_count=5)
        session.status = Session.Status.CLOSED
        session.save(update_fields=("status", "updated_at"))

        registration = update_registration(
            access.registration, comment="Nouvelle précision", at=self.now
        )

        self.assertEqual(registration.comment, "Nouvelle précision")
        with self.assertRaises(SessionUnavailable):
            update_registration(
                registration,
                reservation_requests=[ReservationRequest(session.pk, 6)],
                student_count=6,
                at=self.now,
            )

    def test_confirming_expired_draft_can_exceed_capacity(self):
        session = self.session(time(9), time(10), capacity=10)
        expired = self.draft([ReservationRequest(session.pk, 10)], student_count=10)
        later = self.now + timedelta(hours=2)
        self.draft([ReservationRequest(session.pk, 10)], student_count=10, at=later)

        confirm_registration(expired.registration, at=later)

        expired.registration.refresh_from_db()
        self.assertEqual(expired.registration.status, Registration.Status.CONFIRMED)
        self.assertEqual(reserved_student_count(session, at=later), 20)

    def test_confirmation_is_atomic_and_audited(self):
        session = self.session(time(9), time(10), capacity=10)
        access = self.draft([ReservationRequest(session.pk, 10)], student_count=10)

        registration = confirm_registration(access.registration, at=self.now)

        self.assertEqual(registration.status, Registration.Status.CONFIRMED)
        self.assertIsNone(registration.draft_expires_at)
        self.assertEqual(registration.confirmed_at, self.now)
        self.assertTrue(
            registration.events.filter(event_type=RegistrationEvent.Type.CONFIRMED).exists()
        )

    def test_teacher_cannot_modify_after_deadline_but_staff_can(self):
        session = self.session(time(9), time(10), capacity=10)
        access = self.draft([ReservationRequest(session.pk, 5)], student_count=5)
        after_deadline = datetime.fromisoformat("2026-09-17T00:00:00+02:00")

        with self.assertRaises(RegistrationNotEditable):
            update_registration(access.registration, comment="Trop tard", at=after_deadline)

        registration = update_registration(
            access.registration,
            comment="Correction équipe",
            actor_kind=RegistrationEvent.ActorKind.STAFF,
            at=after_deadline,
        )
        self.assertEqual(registration.comment, "Correction équipe")

    def test_teacher_cannot_create_a_draft_after_deadline(self):
        after_deadline = datetime.fromisoformat("2026-09-17T00:00:00+02:00")

        with self.assertRaisesMessage(RegistrationNotEditable, "closes"):
            self.draft([], student_count=5, at=after_deadline)

    def test_raw_edit_token_is_never_persisted_or_audited(self):
        session = self.session(time(9), time(10), capacity=10)
        access = self.draft([ReservationRequest(session.pk, 5)], student_count=5)
        registration = Registration.objects.get(pk=access.registration.pk)

        self.assertNotEqual(registration.edit_token_digest, access.edit_token)
        audited_changes = list(registration.events.values_list("changes", flat=True))
        self.assertNotIn(access.edit_token, str(audited_changes))
        self.assertEqual(
            get_registration_for_token(
                reference=registration.reference, token=access.edit_token, at=self.now
            ),
            registration,
        )
        with self.assertRaises(InvalidEditToken):
            get_registration_for_token(
                reference=registration.reference, token="incorrect", at=self.now
            )

    def test_rotating_token_revokes_previous_value_without_auditing_new_value(self):
        session = self.session(time(9), time(10), capacity=10)
        access = self.draft([ReservationRequest(session.pk, 5)], student_count=5)

        replacement = rotate_registration_token(access.registration, at=self.now)

        with self.assertRaises(InvalidEditToken):
            get_registration_for_token(
                reference=access.registration.reference,
                token=access.edit_token,
                at=self.now,
            )
        registration = get_registration_for_token(
            reference=access.registration.reference, token=replacement, at=self.now
        )
        token_event = registration.events.get(event_type=RegistrationEvent.Type.TOKEN_ROTATED)
        self.assertNotIn(replacement, str(token_event.changes))

    def test_sensitive_free_text_is_never_copied_to_audit_events(self):
        access = self.draft([], student_count=5)
        sensitive_need = "Traitement médical confidentiel"
        level_comment = "Un élève a besoin d'un accompagnement individuel"
        private_comment = "Numéro personnel et détail privé"

        update_registration(
            access.registration,
            special_needs=sensitive_need,
            level_comment=level_comment,
            comment=private_comment,
            at=self.now,
        )

        audit_payload = str(list(access.registration.events.values_list("changes", flat=True)))
        self.assertNotIn(sensitive_need, audit_payload)
        self.assertNotIn(level_comment, audit_payload)
        self.assertNotIn(private_comment, audit_payload)

    def test_group_metadata_is_created_normalized_and_audited(self):
        family = GroupFamily.objects.create(name="Collèges", slug="colleges")

        access = create_draft(
            institution=self.institution,
            teacher=self.teacher,
            group_name="CM2 A",
            group_code="Truffe Dorée",
            family=family,
            school_level=self.level,
            student_count=20,
            chaperone_count=2,
            visit_date=date(2026, 9, 23),
            level_comment="CM1 et CM2",
            at=self.now,
        )

        self.assertEqual(access.registration.group_code, "truffe-doree")
        self.assertEqual(access.registration.family, family)
        self.assertEqual(access.registration.level_comment, "CM1 et CM2")

        updated = update_registration(
            access.registration,
            group_code="Pomme Vive",
            level_comment="CM2",
            at=self.now,
        )
        event = updated.events.filter(event_type=RegistrationEvent.Type.UPDATED).latest("pk")

        self.assertEqual(updated.group_code, "pomme-vive")
        self.assertEqual(event.changes["group_code"]["to"], "pomme-vive")
        self.assertEqual(event.changes["level_comment"], {"changed": True})

    def test_generated_group_code_retries_a_database_collision(self):
        existing = self.draft([], student_count=5).registration

        with patch(
            "inscriptions.services.registration.generate_unique_group_code",
            side_effect=(existing.group_code, "pomme-vif"),
        ):
            created = self.draft([], student_count=5).registration

        self.assertEqual(created.group_code, "pomme-vif")

    def test_teacher_must_belong_to_registration_institution(self):
        other = Institution.objects.create(
            name="Autre établissement",
            address="2 rue du Test",
            postal_code="44000",
            city="Nantes",
            department="44",
        )
        session = self.session(time(9), time(10))

        with self.assertRaisesMessage(InvalidRegistrationData, "appartenir"):
            create_draft(
                institution=other,
                teacher=self.teacher,
                group_name="Groupe",
                school_level=self.level,
                student_count=5,
                chaperone_count=1,
                visit_date=date(2026, 9, 23),
                reservation_requests=[ReservationRequest(session.pk, 5)],
                at=self.now,
            )
