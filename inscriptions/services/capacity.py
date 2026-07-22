from collections.abc import Mapping

from django.db.models import F, IntegerField, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from catalogue.models import Session
from inscriptions.models import Registration, Reservation


class CapacityError(Exception):
    """Base class for reservation capacity failures."""


class SessionUnavailable(CapacityError):
    def __init__(self, session_id, message="Cette séance n'est pas disponible."):
        self.session_id = session_id
        super().__init__(message)


class CapacityExceeded(CapacityError):
    def __init__(self, session, *, requested: int, available: int):
        self.session = session
        self.requested = requested
        self.available = max(available, 0)
        super().__init__(
            f"Capacité insuffisante pour la séance {session.pk} : "
            f"{requested} participant(s) demandé(s), {self.available} disponible(s)."
        )


def held_reservations(*, at=None):
    """Reservations consuming capacity: confirmed or non-expired drafts."""
    at = at or timezone.now()
    return Reservation.objects.filter(status=Reservation.Status.ACTIVE).filter(
        Q(registration__status=Registration.Status.CONFIRMED)
        | Q(
            registration__status=Registration.Status.DRAFT,
            registration__draft_expires_at__gt=at,
        )
    )


def _participant_sum(queryset) -> int:
    return queryset.aggregate(
        total=Coalesce(
            Sum(
                F("student_count") + F("chaperone_count"),
                output_field=IntegerField(),
            ),
            0,
        )
    )["total"]


def reserved_participant_count(session, *, at=None, excluding_registration_id=None) -> int:
    queryset = held_reservations(at=at).filter(session=session)
    if excluding_registration_id is not None:
        queryset = queryset.exclude(registration_id=excluding_registration_id)
    return _participant_sum(queryset)


def reserved_student_count(session, *, at=None, excluding_registration_id=None) -> int:
    """Alias historique : la valeur inclut désormais les accompagnateurs."""
    return reserved_participant_count(
        session,
        at=at,
        excluding_registration_id=excluding_registration_id,
    )


def remaining_capacity(session, *, at=None, excluding_registration_id=None) -> int:
    reserved = reserved_participant_count(
        session, at=at, excluding_registration_id=excluding_registration_id
    )
    return max(session.max_capacity - reserved, 0)


def lock_sessions(session_ids) -> dict[int, Session]:
    """Lock sessions in primary-key order; caller must be inside atomic()."""
    ids = sorted(set(session_ids))
    sessions = {
        session.pk: session
        for session in Session.objects.select_for_update()
        .select_related("animation")
        .filter(pk__in=ids)
        .order_by("pk")
    }
    missing = set(ids) - sessions.keys()
    if missing:
        missing_id = min(missing)
        raise SessionUnavailable(missing_id, "La séance demandée n'existe pas.")
    return sessions


def assert_capacity(
    sessions: Mapping[int, Session],
    requested_by_session: Mapping[int, int],
    *,
    excluding_registration_id=None,
    at=None,
) -> None:
    """Validate desired participant counts while the supplied sessions are locked."""
    at = at or timezone.now()
    usage = held_reservations(at=at).filter(session_id__in=sessions)
    if excluding_registration_id is not None:
        usage = usage.exclude(registration_id=excluding_registration_id)
    totals = {
        row["session_id"]: row["total"]
        for row in usage.values("session_id").annotate(
            total=Sum(
                F("student_count") + F("chaperone_count"),
                output_field=IntegerField(),
            )
        )
    }
    for session_id in sorted(requested_by_session):
        session = sessions[session_id]
        requested = requested_by_session[session_id]
        available = session.max_capacity - totals.get(session_id, 0)
        if requested > available:
            raise CapacityExceeded(session, requested=requested, available=available)
