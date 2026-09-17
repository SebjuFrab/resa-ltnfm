"""Keep session end times in step with an animation's duration."""

from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Animation, Session


def plan_session_end_times(sessions, duration):
    """Validate all final times before writing any of them; overlaps are allowed."""
    if duration is None or duration < 1:
        raise ValidationError("La durée doit être d'au moins une minute.")
    updates = []
    seen = set()
    for session in sessions:
        end = datetime.combine(session.date, session.starts_at) + timedelta(minutes=duration)
        if end.date() != session.date:
            raise ValidationError(
                f"La séance du {session.date:%d/%m/%Y} à {session.starts_at:%H:%M} "
                f"({session.location}) se terminerait le lendemain. "
                "Les séances doivent se terminer le jour de leur début."
            )
        key = (session.date, session.starts_at, session.location.lower())
        if key in seen:
            raise ValidationError(
                f"Deux séances du {session.date:%d/%m/%Y} à {session.starts_at:%H:%M} "
                f"({session.location}) auraient exactement les mêmes horaires. "
                "Distinguez leurs lieux ou leurs heures de début avant le recalcul."
            )
        seen.add(key)
        if session.ends_at != end.time():
            updates.append((session, end.time()))
    return updates


def recalculate_session_end_times(animation, *, using=None):
    """Recalculate existing slots atomically, preserving their IDs and bookings."""
    using = using or animation._state.db or "default"
    with transaction.atomic(using=using):
        current = Animation.objects.using(using).select_for_update().get(pk=animation.pk)
        sessions = (
            Session.objects.using(using)
            .filter(animation_id=current.pk)
            .select_for_update()
            .order_by("pk")
        )
        updates = plan_session_end_times(sessions, current.indicative_duration)
        updated_at = timezone.now()
        for session, ends_at in updates:
            session.ends_at = ends_at
            session.updated_at = updated_at
        if updates:
            Session.objects.using(using).bulk_update(
                [session for session, _ends_at in updates], ("ends_at", "updated_at")
            )
    return len(updates)
