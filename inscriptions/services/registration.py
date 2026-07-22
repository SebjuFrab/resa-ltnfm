from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from catalogue.models import Session
from inscriptions.codes import generate_unique_group_code, normalize_group_code
from inscriptions.models import Institution, Registration, RegistrationEvent, Reservation, Teacher
from inscriptions.services.capacity import (
    SessionUnavailable,
    assert_capacity,
    lock_sessions,
)
from inscriptions.services.tokens import issue_token


class RegistrationError(Exception):
    """Base class for registration workflow failures."""


class RegistrationNotEditable(RegistrationError):
    pass


class InvalidRegistrationData(RegistrationError):
    pass


class InvalidProgram(RegistrationError):
    pass


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    session_id: int
    student_count: int
    chaperone_count: int = 0

    @property
    def total_participant_count(self):
        return self.student_count + self.chaperone_count


@dataclass(frozen=True, slots=True)
class RegistrationAccess:
    registration: Registration
    edit_token: str


_UNSET = object()


def _at(value):
    return value or timezone.now()


def _registration_id(registration_or_id):
    if isinstance(registration_or_id, Registration):
        return registration_or_id.pk
    return registration_or_id


def _event_date_values():
    return {date.fromisoformat(value) for value in settings.EVENT_DATES}


def _validate_registration_values(
    *,
    institution: Institution,
    teacher: Teacher,
    student_count: int,
    chaperone_count: int,
    visit_date,
):
    if teacher.institution_id != institution.pk:
        raise InvalidRegistrationData(
            "Le professeur doit appartenir à l'établissement de l'inscription."
        )
    if not isinstance(student_count, int) or isinstance(student_count, bool) or student_count <= 0:
        raise InvalidRegistrationData("Le nombre d'élèves doit être strictement positif.")
    invalid_chaperone_count = (
        not isinstance(chaperone_count, int)
        or isinstance(chaperone_count, bool)
        or chaperone_count < 0
    )
    if invalid_chaperone_count:
        raise InvalidRegistrationData("Le nombre d'accompagnateurs ne peut pas être négatif.")
    if visit_date not in _event_date_values():
        raise InvalidRegistrationData("Le jour de visite doit être le 23 ou le 24 septembre 2026.")


def _normalize_requests(requests: Iterable[ReservationRequest]) -> dict[int, ReservationRequest]:
    normalized = {}
    for request in requests:
        if not isinstance(request, ReservationRequest):
            try:
                request = ReservationRequest(**request)
            except (TypeError, ValueError) as exc:
                raise InvalidProgram("Une réservation demandée est invalide.") from exc
        if request.session_id in normalized:
            raise InvalidProgram("Une séance ne peut être sélectionnée qu'une seule fois.")
        if (
            not isinstance(request.student_count, int)
            or isinstance(request.student_count, bool)
            or request.student_count <= 0
        ):
            raise InvalidProgram("L'effectif d'une séance doit être strictement positif.")
        if (
            not isinstance(request.chaperone_count, int)
            or isinstance(request.chaperone_count, bool)
            or request.chaperone_count < 0
        ):
            raise InvalidProgram("L'effectif accompagnateur ne peut pas être négatif.")
        normalized[request.session_id] = request
    return normalized


def _assert_sessions_available(sessions, requests, *, visit_date, existing_requests=None):
    existing_requests = existing_requests or {}
    for session_id in sorted(requests):
        session = sessions[session_id]
        request = requests[session_id]
        previous = existing_requests.get(session_id)
        closed_without_increase = (
            session.status == Session.Status.CLOSED
            and previous is not None
            and request.student_count <= previous.student_count
            and request.chaperone_count <= previous.chaperone_count
        )
        if not session.animation.is_active:
            raise SessionUnavailable(session_id, "L'animation de cette séance est inactive.")
        if session.status != Session.Status.OPEN and not closed_without_increase:
            raise SessionUnavailable(session_id, "Cette séance est fermée ou annulée.")
        if session.date != visit_date:
            raise InvalidProgram("Toutes les séances doivent avoir lieu le jour de visite choisi.")


def _assert_concurrent_count(sessions, requests, *, field_name, maximum, message):
    events = []
    for session_id, request in requests.items():
        session = sessions[session_id]
        requested_count = getattr(request, field_name)
        events.append((session.starts_at, 1, requested_count))
        events.append((session.ends_at, 0, -requested_count))

    concurrent = 0
    for _boundary, _end_before_start, delta in sorted(events):
        concurrent += delta
        if concurrent > maximum:
            raise InvalidProgram(message)


def _assert_program(sessions, requests, *, student_count: int, chaperone_count: int = 0):
    """Sweep [start, end) boundaries so adjacent sessions never overlap."""
    _assert_concurrent_count(
        sessions,
        requests,
        field_name="student_count",
        maximum=student_count,
        message=(
            "L'effectif étudiant affecté à des séances simultanées dépasse "
            "l'effectif étudiant du groupe."
        ),
    )
    _assert_concurrent_count(
        sessions,
        requests,
        field_name="chaperone_count",
        maximum=chaperone_count,
        message=(
            "L'effectif accompagnateur affecté à des séances simultanées dépasse "
            "l'effectif accompagnateur du groupe."
        ),
    )


def _requested_capacities(requests):
    return {session_id: request.total_participant_count for session_id, request in requests.items()}


def _snapshot_requests(active_reservations):
    return {
        reservation.session_id: ReservationRequest(
            session_id=reservation.session_id,
            student_count=reservation.student_count,
            chaperone_count=reservation.chaperone_count,
        )
        for reservation in active_reservations
    }


def _sync_reservations(registration, requests, active_reservations, *, at):
    active_by_session = {reservation.session_id: reservation for reservation in active_reservations}
    removed_ids = set(active_by_session) - set(requests)
    if removed_ids:
        Reservation.objects.filter(
            pk__in=[active_by_session[session_id].pk for session_id in removed_ids]
        ).update(status=Reservation.Status.CANCELLED, cancelled_at=at, updated_at=at)

    for session_id, request in requests.items():
        reservation = active_by_session.get(session_id)
        if reservation is None:
            Reservation.objects.create(
                registration=registration,
                session_id=session_id,
                student_count=request.student_count,
                chaperone_count=request.chaperone_count,
            )
            continue
        changed = (
            reservation.student_count != request.student_count
            or reservation.chaperone_count != request.chaperone_count
        )
        if changed:
            reservation.student_count = request.student_count
            reservation.chaperone_count = request.chaperone_count
            reservation.save(update_fields=("student_count", "chaperone_count", "updated_at"))


def _changes(old_values: dict[str, Any], new_values: dict[str, Any]):
    result = {}
    for field, old in old_values.items():
        new = new_values[field]
        if old != new:
            if field in {"special_needs", "level_comment", "comment"}:
                result[field] = {"changed": True}
            else:
                result[field] = {
                    "from": old.isoformat() if hasattr(old, "isoformat") else old,
                    "to": new.isoformat() if hasattr(new, "isoformat") else new,
                }
    return result


def _create_registration(*, group_code=None, **values):
    """Crée une inscription en réessayant uniquement les collisions de code."""
    supplied_code = normalize_group_code(group_code) if group_code else ""
    attempts = 1 if supplied_code else 20

    for _attempt in range(attempts):
        candidate = supplied_code or generate_unique_group_code()
        try:
            # Le savepoint permet à la transaction englobante de poursuivre après
            # une collision concurrente sur le code lisible.
            with transaction.atomic():
                return Registration.objects.create(
                    group_code=candidate,
                    **values,
                )
        except IntegrityError as error:
            collision = Registration.objects.filter(group_code=candidate).exists()
            if supplied_code or not collision:
                if collision:
                    raise InvalidRegistrationData("Ce code de groupe est déjà utilisé.") from error
                raise
    raise InvalidRegistrationData("Impossible de générer un code de groupe disponible. Réessayez.")


@transaction.atomic
def create_draft(
    *,
    institution: Institution,
    teacher: Teacher,
    group_name: str,
    school_level,
    student_count: int,
    chaperone_count: int,
    visit_date,
    reservation_requests: Iterable[ReservationRequest] = (),
    special_needs: str = "",
    comment: str = "",
    family=None,
    group_code: str | None = None,
    level_comment: str = "",
    actor_kind=RegistrationEvent.ActorKind.TEACHER,
    actor_user=None,
    at=None,
) -> RegistrationAccess:
    at = _at(at)
    if (
        actor_kind == RegistrationEvent.ActorKind.TEACHER
        and at > settings.REGISTRATION_EDIT_DEADLINE
    ):
        raise RegistrationNotEditable("Les inscriptions en ligne sont closes.")
    _validate_registration_values(
        institution=institution,
        teacher=teacher,
        student_count=student_count,
        chaperone_count=chaperone_count,
        visit_date=visit_date,
    )
    requests = _normalize_requests(reservation_requests)
    sessions = lock_sessions(requests)
    _assert_sessions_available(sessions, requests, visit_date=visit_date)
    _assert_program(
        sessions,
        requests,
        student_count=student_count,
        chaperone_count=chaperone_count,
    )
    assert_capacity(sessions, _requested_capacities(requests), at=at)

    token = issue_token()
    registration = _create_registration(
        institution=institution,
        teacher=teacher,
        group_code=group_code,
        group_name=group_name,
        family=family,
        school_level=school_level,
        student_count=student_count,
        chaperone_count=chaperone_count,
        visit_date=visit_date,
        special_needs=special_needs,
        level_comment=level_comment,
        comment=comment,
        status=Registration.Status.DRAFT,
        draft_expires_at=at + timedelta(minutes=settings.DRAFT_HOLD_MINUTES),
        edit_token_digest=token.digest,
        token_created_at=at,
    )
    _sync_reservations(registration, requests, [], at=at)
    RegistrationEvent.objects.create(
        registration=registration,
        event_type=RegistrationEvent.Type.CREATED,
        actor_kind=actor_kind,
        actor_user=actor_user,
        changes={"status": Registration.Status.DRAFT},
    )
    return RegistrationAccess(registration=registration, edit_token=token.value)


@transaction.atomic
def update_registration(
    registration_or_id,
    *,
    institution=_UNSET,
    teacher=_UNSET,
    group_name=_UNSET,
    school_level=_UNSET,
    student_count=_UNSET,
    chaperone_count=_UNSET,
    visit_date=_UNSET,
    reservation_requests: Iterable[ReservationRequest] | None = None,
    special_needs=_UNSET,
    comment=_UNSET,
    family=_UNSET,
    group_code=_UNSET,
    level_comment=_UNSET,
    actor_kind=RegistrationEvent.ActorKind.TEACHER,
    actor_user=None,
    at=None,
) -> Registration:
    at = _at(at)
    registration = (
        Registration.objects.select_for_update()
        .select_related("institution", "teacher", "school_level", "family")
        .get(pk=_registration_id(registration_or_id))
    )
    if registration.status == Registration.Status.CANCELLED:
        raise RegistrationNotEditable("Une inscription annulée ne peut plus être modifiée.")
    if (
        actor_kind == RegistrationEvent.ActorKind.TEACHER
        and at > settings.REGISTRATION_EDIT_DEADLINE
    ):
        raise RegistrationNotEditable("La date limite de modification est dépassée.")

    field_values = {
        "institution": registration.institution if institution is _UNSET else institution,
        "teacher": registration.teacher if teacher is _UNSET else teacher,
        "group_code": registration.group_code if group_code is _UNSET else group_code,
        "group_name": registration.group_name if group_name is _UNSET else group_name,
        "family": registration.family if family is _UNSET else family,
        "school_level": registration.school_level if school_level is _UNSET else school_level,
        "student_count": registration.student_count if student_count is _UNSET else student_count,
        "chaperone_count": (
            registration.chaperone_count if chaperone_count is _UNSET else chaperone_count
        ),
        "visit_date": registration.visit_date if visit_date is _UNSET else visit_date,
        "special_needs": registration.special_needs if special_needs is _UNSET else special_needs,
        "level_comment": (registration.level_comment if level_comment is _UNSET else level_comment),
        "comment": registration.comment if comment is _UNSET else comment,
    }
    field_values["group_code"] = normalize_group_code(field_values["group_code"])
    if not field_values["group_code"]:
        field_values["group_code"] = generate_unique_group_code(
            excluding_registration_id=registration.pk
        )
    if (
        Registration.objects.exclude(pk=registration.pk)
        .filter(group_code=field_values["group_code"])
        .exists()
    ):
        raise InvalidRegistrationData("Ce code de groupe est déjà utilisé.")
    _validate_registration_values(
        institution=field_values["institution"],
        teacher=field_values["teacher"],
        student_count=field_values["student_count"],
        chaperone_count=field_values["chaperone_count"],
        visit_date=field_values["visit_date"],
    )

    active = list(
        Reservation.objects.filter(
            registration=registration, status=Reservation.Status.ACTIVE
        ).select_related("session")
    )
    existing_requests = _snapshot_requests(active)
    requests = (
        existing_requests
        if reservation_requests is None
        else _normalize_requests(reservation_requests)
    )
    sessions = lock_sessions(set(requests) | {reservation.session_id for reservation in active})
    _assert_sessions_available(
        sessions,
        requests,
        visit_date=field_values["visit_date"],
        existing_requests=existing_requests,
    )
    _assert_program(
        sessions,
        requests,
        student_count=field_values["student_count"],
        chaperone_count=field_values["chaperone_count"],
    )
    assert_capacity(
        sessions,
        _requested_capacities(requests),
        excluding_registration_id=registration.pk,
        at=at,
    )

    old_values = {
        "institution_id": registration.institution_id,
        "teacher_id": registration.teacher_id,
        "group_code": registration.group_code,
        "group_name": registration.group_name,
        "family_id": registration.family_id,
        "school_level_id": registration.school_level_id,
        "student_count": registration.student_count,
        "chaperone_count": registration.chaperone_count,
        "visit_date": registration.visit_date,
        "special_needs": registration.special_needs,
        "level_comment": registration.level_comment,
        "comment": registration.comment,
    }
    for field, value in field_values.items():
        setattr(registration, field, value)
    if registration.status == Registration.Status.DRAFT:
        registration.draft_expires_at = at + timedelta(minutes=settings.DRAFT_HOLD_MINUTES)
    try:
        with transaction.atomic():
            registration.save()
    except IntegrityError as error:
        if (
            Registration.objects.exclude(pk=registration.pk)
            .filter(group_code=registration.group_code)
            .exists()
        ):
            raise InvalidRegistrationData("Ce code de groupe est déjà utilisé.") from error
        raise
    _sync_reservations(registration, requests, active, at=at)

    new_values = {
        "institution_id": registration.institution_id,
        "teacher_id": registration.teacher_id,
        "group_code": registration.group_code,
        "group_name": registration.group_name,
        "family_id": registration.family_id,
        "school_level_id": registration.school_level_id,
        "student_count": registration.student_count,
        "chaperone_count": registration.chaperone_count,
        "visit_date": registration.visit_date,
        "special_needs": registration.special_needs,
        "level_comment": registration.level_comment,
        "comment": registration.comment,
    }
    event_changes = _changes(old_values, new_values)
    event_changes["reservations"] = [
        {
            "session_id": request.session_id,
            "student_count": request.student_count,
            "chaperone_count": request.chaperone_count,
        }
        for request in requests.values()
    ]
    RegistrationEvent.objects.create(
        registration=registration,
        event_type=RegistrationEvent.Type.UPDATED,
        actor_kind=actor_kind,
        actor_user=actor_user,
        changes=event_changes,
    )
    return registration


@transaction.atomic
def save_draft(registration_or_id, **kwargs) -> Registration:
    registration_id = _registration_id(registration_or_id)
    registration = Registration.objects.select_for_update().get(pk=registration_id)
    if registration.status != Registration.Status.DRAFT:
        raise RegistrationNotEditable("Cette inscription n'est pas un brouillon modifiable.")
    return update_registration(registration, **kwargs)


@transaction.atomic
def confirm_registration(
    registration_or_id,
    *,
    actor_kind=RegistrationEvent.ActorKind.TEACHER,
    actor_user=None,
    at=None,
) -> Registration:
    at = _at(at)
    registration = Registration.objects.select_for_update().get(
        pk=_registration_id(registration_or_id)
    )
    if registration.status == Registration.Status.CONFIRMED:
        return registration
    if registration.status != Registration.Status.DRAFT:
        raise RegistrationNotEditable("Cette inscription ne peut pas être confirmée.")
    if (
        actor_kind == RegistrationEvent.ActorKind.TEACHER
        and at > settings.REGISTRATION_EDIT_DEADLINE
    ):
        raise RegistrationNotEditable("La date limite de confirmation est dépassée.")

    active = list(
        Reservation.objects.filter(
            registration=registration, status=Reservation.Status.ACTIVE
        ).select_related("session")
    )
    if not active:
        raise InvalidProgram("Au moins une séance doit être choisie avant la confirmation.")
    requests = _snapshot_requests(active)
    sessions = lock_sessions(requests)
    _assert_sessions_available(sessions, requests, visit_date=registration.visit_date)
    _assert_program(
        sessions,
        requests,
        student_count=registration.student_count,
        chaperone_count=registration.chaperone_count,
    )
    assert_capacity(
        sessions,
        _requested_capacities(requests),
        excluding_registration_id=registration.pk,
        at=at,
    )
    registration.status = Registration.Status.CONFIRMED
    registration.draft_expires_at = None
    registration.confirmed_at = at
    registration.save(update_fields=("status", "draft_expires_at", "confirmed_at", "updated_at"))
    RegistrationEvent.objects.create(
        registration=registration,
        event_type=RegistrationEvent.Type.CONFIRMED,
        actor_kind=actor_kind,
        actor_user=actor_user,
        changes={
            "status": {
                "from": Registration.Status.DRAFT,
                "to": Registration.Status.CONFIRMED,
            }
        },
    )
    return registration


@transaction.atomic
def cancel_registration(
    registration_or_id,
    *,
    actor_kind=RegistrationEvent.ActorKind.TEACHER,
    actor_user=None,
    at=None,
) -> Registration:
    at = _at(at)
    registration = Registration.objects.select_for_update().get(
        pk=_registration_id(registration_or_id)
    )
    if registration.status == Registration.Status.CANCELLED:
        return registration
    if (
        actor_kind == RegistrationEvent.ActorKind.TEACHER
        and at > settings.REGISTRATION_EDIT_DEADLINE
    ):
        raise RegistrationNotEditable("La date limite d'annulation est dépassée.")
    active = list(
        Reservation.objects.filter(registration=registration, status=Reservation.Status.ACTIVE)
    )
    lock_sessions(reservation.session_id for reservation in active)
    Reservation.objects.filter(pk__in=[reservation.pk for reservation in active]).update(
        status=Reservation.Status.CANCELLED, cancelled_at=at, updated_at=at
    )
    previous_status = registration.status
    registration.status = Registration.Status.CANCELLED
    registration.draft_expires_at = None
    registration.cancelled_at = at
    registration.token_revoked_at = at
    registration.save(
        update_fields=(
            "status",
            "draft_expires_at",
            "cancelled_at",
            "token_revoked_at",
            "updated_at",
        )
    )
    RegistrationEvent.objects.create(
        registration=registration,
        event_type=RegistrationEvent.Type.CANCELLED,
        actor_kind=actor_kind,
        actor_user=actor_user,
        changes={"status": {"from": previous_status, "to": Registration.Status.CANCELLED}},
    )
    return registration
