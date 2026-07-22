from django.core.exceptions import ValidationError
from django.db.models import F, IntegerField, Sum

from inscriptions.services.capacity import held_reservations


def held_participant_count(session_id, *, at=None):
    return (
        held_reservations(at=at)
        .filter(session_id=session_id)
        .aggregate(
            total=Sum(
                F("student_count") + F("chaperone_count"),
                output_field=IntegerField(),
            )
        )["total"]
        or 0
    )


def held_student_count(session_id, *, at=None):
    """Alias historique : la valeur représente désormais tous les participants."""
    return held_participant_count(session_id, at=at)


def validate_max_capacity(session_id, max_capacity, *, at=None):
    reserved = held_participant_count(session_id, at=at)
    if max_capacity < reserved:
        raise ValidationError(
            f"La capacité ne peut pas être inférieure aux {reserved} places actuellement réservées."
        )
