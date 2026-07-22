import csv
from datetime import date

from django.http import HttpResponse
from django.utils import timezone

from catalogue.models import Session
from inscriptions.models import Registration, Reservation

FORMULA_PREFIXES = ("=", "+", "-", "@")


def _safe_cell(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if text.lstrip().startswith(FORMULA_PREFIXES):
        return f"'{text}"
    return text


def _csv_response(filename, headers, rows, *, delimiter=";"):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_safe_cell(value) for value in row])
    return response


def registrations_csv(*, visit_date: date | None = None, delimiter=";"):
    registrations = Registration.objects.select_related(
        "institution", "teacher", "school_level", "family"
    ).order_by("visit_date", "institution__name", "group_name")
    if visit_date:
        registrations = registrations.filter(visit_date=visit_date)

    rows = (
        (
            registration.institution.name,
            registration.institution.get_institution_type_display(),
            f"{registration.teacher.first_name} {registration.teacher.last_name}",
            registration.teacher.email,
            registration.teacher.phone,
            registration.group_name,
            registration.group_code,
            registration.family.name if registration.family_id else "",
            registration.institution.city,
            registration.institution.department,
            registration.school_level.label,
            registration.level_comment,
            registration.student_count,
            registration.chaperone_count,
            registration.total_participant_count,
            registration.visit_date.isoformat(),
            registration.get_status_display(),
            registration.reference,
            registration.special_needs,
            registration.comment,
        )
        for registration in registrations
    )
    return _csv_response(
        "inscriptions.csv",
        (
            "Établissement",
            "Type d'établissement",
            "Professeur",
            "Courriel",
            "Téléphone",
            "Groupe",
            "Code groupe",
            "Famille",
            "Commune",
            "Département",
            "Niveau",
            "Remarque niveau",
            "Nombre d'élèves",
            "Nombre d'accompagnateurs",
            "Effectif total",
            "Jour",
            "Statut",
            "Référence",
            "Besoins particuliers",
            "Remarque générale",
        ),
        rows,
        delimiter=delimiter,
    )


def reservations_csv(*, visit_date: date | None = None, delimiter=";"):
    reservations = Reservation.objects.filter(
        status=Reservation.Status.ACTIVE,
        registration__status=Registration.Status.CONFIRMED,
    ).select_related(
        "registration__institution",
        "registration__teacher",
        "registration__school_level",
        "registration__family",
        "session__animation",
    )
    if visit_date:
        reservations = reservations.filter(session__date=visit_date)
    reservations = reservations.order_by(
        "session__date",
        "session__starts_at",
        "session__animation__title",
        "registration__institution__name",
    )

    rows = (
        (
            reservation.session.date.isoformat(),
            reservation.session.starts_at.strftime("%H:%M"),
            reservation.session.ends_at.strftime("%H:%M"),
            reservation.session.animation.title,
            reservation.session.location,
            reservation.registration.institution.name,
            reservation.registration.group_name,
            reservation.registration.group_code,
            (
                reservation.registration.family.name
                if reservation.registration.family_id
                else ""
            ),
            reservation.registration.institution.city,
            reservation.registration.institution.department,
            reservation.registration.school_level.label,
            reservation.registration.teacher.email,
            reservation.student_count,
            reservation.chaperone_count,
            reservation.total_participant_count,
        )
        for reservation in reservations
    )
    return _csv_response(
        "reservations.csv",
        (
            "Jour",
            "Heure de début",
            "Heure de fin",
            "Animation",
            "Lieu",
            "Établissement",
            "Groupe",
            "Code groupe",
            "Famille",
            "Commune",
            "Département",
            "Niveau",
            "Courriel du professeur",
            "Nombre d'élèves",
            "Nombre d'accompagnateurs",
            "Effectif total",
        ),
        rows,
        delimiter=delimiter,
    )


def sessions_csv(*, visit_date: date | None = None, delimiter=";"):
    sessions = Session.objects.with_capacities(at=timezone.now()).select_related("animation")
    if visit_date:
        sessions = sessions.filter(date=visit_date)
    sessions = sessions.order_by("date", "starts_at", "animation__title")

    def session_rows():
        for session in sessions:
            reservations = session.reservations.filter(
                status=Reservation.Status.ACTIVE,
                registration__status=Registration.Status.CONFIRMED,
            ).select_related(
                "registration__institution",
                "registration__teacher",
            )
            reservations = reservations.order_by(
                "registration__institution__name", "registration__group_name"
            )
            if not reservations:
                yield (
                    session.date.isoformat(),
                    session.starts_at.strftime("%H:%M"),
                    session.ends_at.strftime("%H:%M"),
                    session.animation.title,
                    session.location,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    session.reserved_capacity,
                    session.max_capacity,
                    session.remaining_capacity,
                )
                continue
            for reservation in reservations:
                registration = reservation.registration
                yield (
                    session.date.isoformat(),
                    session.starts_at.strftime("%H:%M"),
                    session.ends_at.strftime("%H:%M"),
                    session.animation.title,
                    session.location,
                    registration.institution.name,
                    registration.group_name,
                    registration.group_code,
                    f"{registration.teacher.first_name} {registration.teacher.last_name}",
                    reservation.student_count,
                    reservation.chaperone_count,
                    reservation.total_participant_count,
                    session.reserved_capacity,
                    session.max_capacity,
                    session.remaining_capacity,
                )

    return _csv_response(
        "seances.csv",
        (
            "Jour",
            "Heure de début",
            "Heure de fin",
            "Animation",
            "Lieu",
            "Établissement",
            "Groupe",
            "Code groupe",
            "Professeur",
            "Nombre d'élèves",
            "Nombre d'accompagnateurs",
            "Effectif du groupe",
            "Total réservé",
            "Capacité maximale",
            "Capacité restante",
        ),
        session_rows(),
        delimiter=delimiter,
    )
