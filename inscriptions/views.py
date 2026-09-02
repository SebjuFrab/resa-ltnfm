from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from catalogue.models import Session
from communication.models import EmailLog
from communication.services import schedule_registration_email
from inscriptions.forms import (
    CancellationForm,
    ConfirmationForm,
    PlanningFilterForm,
    PlanningForm,
    RegistrationIdentityForm,
    RegistrationUpdateForm,
)
from inscriptions.models import (
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
    Teacher,
)
from inscriptions.security import rate_limit
from inscriptions.services.capacity import CapacityError, capacity_warnings
from inscriptions.services.registration import (
    RegistrationError,
    ReservationRequest,
    cancel_registration,
    confirm_registration,
    create_draft,
    save_draft,
    update_registration,
)
from inscriptions.services.tokens import (
    InvalidEditToken,
    get_registration_for_token,
    rotate_registration_token,
)

DRAFTS_SESSION_KEY = "owned_registration_drafts"
MANAGED_SESSION_KEY = "managed_registration"
COMPLETED_SESSION_KEY = "completed_registrations"


def _remember_reference(request, key, reference):
    values = list(request.session.get(key, []))
    value = str(reference)
    if value not in values:
        values.append(value)
    request.session[key] = values[-10:]


def _forget_reference(request, key, reference):
    value = str(reference)
    request.session[key] = [item for item in request.session.get(key, []) if item != value]


def _owned_draft(request, reference):
    if str(reference) not in request.session.get(DRAFTS_SESSION_KEY, []):
        raise Http404
    return get_object_or_404(
        Registration.objects.select_related("institution", "teacher", "school_level"),
        reference=reference,
        status=Registration.Status.DRAFT,
    )


def _managed_registration(request):
    access = request.session.get(MANAGED_SESSION_KEY)
    if not isinstance(access, dict) or not access.get("reference"):
        raise PermissionDenied("Utilisez le lien sécurisé reçu par courriel.")
    registration = get_object_or_404(
        Registration.objects.select_related("institution", "teacher", "school_level"),
        reference=access["reference"],
    )
    current_version = registration.token_created_at.isoformat()
    if registration.token_revoked_at is not None or access.get("token_version") != current_version:
        request.session.pop(MANAGED_SESSION_KEY, None)
        raise PermissionDenied("Votre accès a été révoqué. Utilisez le dernier lien reçu.")
    return registration


def _set_managed_registration(request, registration):
    request.session[MANAGED_SESSION_KEY] = {
        "reference": str(registration.reference),
        "token_version": registration.token_created_at.isoformat(),
    }
    request.session.set_expiry(settings.MANAGEMENT_SESSION_SECONDS)


def _registration_sessions(registration):
    return (
        Session.objects.with_capacities()
        .filter(date=registration.visit_date, animation__is_active=True)
        .select_related("animation", "animation__category")
        .prefetch_related("animation__recommended_levels")
        .order_by("starts_at", "ends_at", "animation__title")
    )


def _merge_reservation_requests(registration, displayed_sessions, submitted_counts):
    displayed_ids = {session.pk for session in displayed_sessions}
    counts = {
        reservation.session_id: (
            reservation.student_count,
            reservation.chaperone_count,
        )
        for reservation in registration.reservations.filter(status=Reservation.Status.ACTIVE)
        if reservation.session_id not in displayed_ids
    }
    counts.update(
        {
            session_id: (student_count, registration.chaperone_count)
            for session_id, student_count in submitted_counts.items()
        }
    )
    return [
        ReservationRequest(
            session_id=session_id,
            student_count=student_count,
            chaperone_count=chaperone_count,
        )
        for session_id, (student_count, chaperone_count) in counts.items()
        if student_count > 0
    ]


def _over_capacity_warnings(registration, reservation_requests):
    return capacity_warnings(
        {
            reservation.session_id: reservation.total_participant_count
            for reservation in reservation_requests
        },
        excluding_registration_id=registration.pk,
    )


def _over_capacity_needs_confirmation(request, registration, reservation_requests):
    warnings = _over_capacity_warnings(registration, reservation_requests)
    needs_confirmation = bool(warnings) and request.POST.get("confirm_over_capacity") != "yes"
    return warnings, needs_confirmation


def _planning_context(request, registration, *, action_url):
    filter_form = PlanningFilterForm(request.GET or None)
    sessions = filter_form.apply(_registration_sessions(registration))
    planning_form = PlanningForm(
        request.POST or None,
        registration=registration,
        sessions=sessions,
    )
    return (
        filter_form,
        sessions,
        planning_form,
        {
            "registration": registration,
            "filter_form": filter_form,
            "planning_form": planning_form,
            "session_rows": list(zip(sessions, planning_form, strict=True)),
            "action_url": action_url,
        },
    )


@sensitive_variables("token")
def _edit_url(request, registration, token):
    landing_url = request.build_absolute_uri(
        reverse("registration-edit-link", kwargs={"reference": registration.reference})
    )
    # URL fragments are never sent in HTTP requests and therefore stay out of access logs.
    return f"{landing_url}#{token}"


@sensitive_variables("token")
def _schedule_email_with_rotated_link(request, registration, kind):
    token = rotate_registration_token(registration)
    registration.refresh_from_db(fields=("token_created_at", "token_revoked_at"))
    _set_managed_registration(request, registration)
    schedule_registration_email(
        registration,
        kind,
        edit_url=_edit_url(request, registration, token),
    )


@rate_limit("registration-start", attempts=20, window_seconds=900)
@require_http_methods(["GET", "POST"])
def registration_start(request):
    if timezone.now() > settings.REGISTRATION_EDIT_DEADLINE:
        return render(
            request,
            "inscriptions/closed.html",
            {"edit_deadline": settings.REGISTRATION_EDIT_DEADLINE},
        )
    form = RegistrationIdentityForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                institution = data["existing_institution"]
                if institution is None:
                    institution = Institution.objects.create(
                        name=data["institution_name"],
                        institution_type=data["institution_type"],
                        address=data["institution_address"],
                        postal_code=data["institution_postal_code"],
                        city=data["institution_city"],
                        department=data["institution_department"],
                        phone=data["institution_phone"],
                        administrative_email=data["institution_email"],
                    )
                teacher = Teacher.objects.create(
                    institution=institution,
                    first_name=data["teacher_first_name"],
                    last_name=data["teacher_last_name"],
                    email=data["teacher_email"],
                    phone=data["teacher_phone"],
                )
                access = create_draft(
                    institution=institution,
                    teacher=teacher,
                    group_name=data["group_name"],
                    school_level=data["school_level"],
                    student_count=data["student_count"],
                    chaperone_count=data["chaperone_count"],
                    visit_date=data["visit_date"],
                    special_needs=data["special_needs"],
                    comment=data["comment"],
                )
        except RegistrationError as error:
            form.add_error(None, str(error))
        else:
            _remember_reference(request, DRAFTS_SESSION_KEY, access.registration.reference)
            messages.success(
                request,
                "Le groupe est enregistré en brouillon. "
                "Vos places seront maintenues pendant une heure.",
            )
            return redirect("registration-planning", reference=access.registration.reference)
    return render(request, "inscriptions/start.html", {"form": form})


@require_http_methods(["GET", "POST"])
def draft_planning(request, reference):
    registration = _owned_draft(request, reference)
    filter_form, sessions, form, context = _planning_context(
        request,
        registration,
        action_url=reverse("registration-planning", kwargs={"reference": reference}),
    )
    if request.method == "POST" and form.is_valid():
        requests = _merge_reservation_requests(registration, sessions, form.submitted_counts())
        warnings, needs_confirmation = _over_capacity_needs_confirmation(
            request, registration, requests
        )
        if needs_confirmation:
            context["over_capacity_warnings"] = warnings
            context["session_rows"] = list(zip(sessions, form, strict=True))
            return render(request, "inscriptions/planning.html", context)
        try:
            save_draft(registration, reservation_requests=requests)
        except (RegistrationError, CapacityError) as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request,
                "Le planning est enregistré. Le maintien des places repart pour une heure.",
            )
            return redirect("registration-review", reference=reference)
    context["session_rows"] = list(zip(sessions, form, strict=True))
    return render(request, "inscriptions/planning.html", context)


@require_http_methods(["GET", "POST"])
def draft_review(request, reference):
    registration = _owned_draft(request, reference)
    reservations = registration.reservations.filter(
        status=Reservation.Status.ACTIVE
    ).select_related("session", "session__animation")
    form = ConfirmationForm(request.POST or None)
    over_capacity_warnings = _over_capacity_warnings(
        registration,
        [
            ReservationRequest(
                session_id=reservation.session_id,
                student_count=reservation.student_count,
                chaperone_count=reservation.chaperone_count,
            )
            for reservation in reservations
        ],
    )
    if request.method == "POST" and form.is_valid():
        try:
            registration = confirm_registration(registration)
            _schedule_email_with_rotated_link(request, registration, EmailLog.Kind.CONFIRMATION)
        except (RegistrationError, CapacityError) as error:
            form.add_error(None, str(error))
        else:
            _forget_reference(request, DRAFTS_SESSION_KEY, reference)
            _remember_reference(request, COMPLETED_SESSION_KEY, reference)
            messages.success(request, "Votre inscription est confirmée.")
            return redirect("registration-complete", reference=reference)
    return render(
        request,
        "inscriptions/review.html",
        {
            "registration": registration,
            "reservations": reservations,
            "form": form,
            "over_capacity_warnings": over_capacity_warnings,
        },
    )


def registration_complete(request, reference):
    allowed = str(reference) in request.session.get(COMPLETED_SESSION_KEY, [])
    managed = request.session.get(MANAGED_SESSION_KEY, {})
    allowed = allowed or (isinstance(managed, dict) and managed.get("reference") == str(reference))
    if not allowed:
        raise Http404
    registration = get_object_or_404(
        Registration.objects.select_related("institution", "teacher", "school_level"),
        reference=reference,
    )
    reservations = registration.reservations.filter(
        status=Reservation.Status.ACTIVE
    ).select_related("session", "session__animation")
    return render(
        request,
        "inscriptions/complete.html",
        {"registration": registration, "reservations": reservations},
    )


@sensitive_post_parameters("token")
@sensitive_variables("token")
@rate_limit("edit-link", attempts=30, window_seconds=900)
@require_http_methods(["GET", "POST"])
def edit_link_entry(request, reference):
    if request.method == "GET":
        return render(
            request,
            "inscriptions/token_entry.html",
            {"registration_reference": reference},
        )
    token = request.POST.get("token", "")
    try:
        registration = get_registration_for_token(reference=reference, token=token)
    except InvalidEditToken as error:
        return render(
            request,
            "inscriptions/invalid_link.html",
            {"reason": str(error)},
            status=404,
        )
    request.session.cycle_key()
    _set_managed_registration(request, registration)
    return redirect("registration-manage")


def manage_registration(request):
    registration = _managed_registration(request)
    reservations = registration.reservations.filter(
        status=Reservation.Status.ACTIVE
    ).select_related("session", "session__animation")
    return render(
        request,
        "inscriptions/manage.html",
        {
            "registration": registration,
            "reservations": reservations,
            "editable": (
                registration.status != Registration.Status.CANCELLED
                and timezone.now() <= settings.REGISTRATION_EDIT_DEADLINE
            ),
            "edit_deadline": settings.REGISTRATION_EDIT_DEADLINE,
        },
    )


@require_http_methods(["GET", "POST"])
def manage_details(request):
    registration = _managed_registration(request)
    if timezone.now() > settings.REGISTRATION_EDIT_DEADLINE:
        raise PermissionDenied("La date limite de modification est dépassée.")
    form = RegistrationUpdateForm(request.POST or None, instance=registration)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                teacher = registration.teacher
                teacher_values = {
                    "first_name": data["teacher_first_name"],
                    "last_name": data["teacher_last_name"],
                    "email": data["teacher_email"],
                    "phone": data["teacher_phone"],
                }
                changed_teacher_fields = [
                    field
                    for field, value in teacher_values.items()
                    if getattr(teacher, field) != value
                ]
                if changed_teacher_fields:
                    if teacher.registrations.exclude(pk=registration.pk).exists():
                        teacher = Teacher.objects.create(
                            institution=registration.institution,
                            **teacher_values,
                        )
                    else:
                        for field, value in teacher_values.items():
                            setattr(teacher, field, value)
                        teacher.save(update_fields=(*changed_teacher_fields, "updated_at"))
                registration = update_registration(
                    registration,
                    teacher=teacher,
                    group_name=data["group_name"],
                    school_level=data["school_level"],
                    student_count=data["student_count"],
                    chaperone_count=data["chaperone_count"],
                    special_needs=data["special_needs"],
                    comment=data["comment"],
                )
                if changed_teacher_fields:
                    RegistrationEvent.objects.create(
                        registration=registration,
                        event_type=RegistrationEvent.Type.UPDATED,
                        actor_kind=RegistrationEvent.ActorKind.TEACHER,
                        changes={"teacher_fields": sorted(changed_teacher_fields)},
                    )
                _schedule_email_with_rotated_link(request, registration, EmailLog.Kind.MODIFICATION)
        except (RegistrationError, CapacityError) as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "Les informations et le lien sécurisé ont été actualisés.")
            return redirect("registration-manage")
    return render(
        request,
        "inscriptions/update_details.html",
        {"registration": registration, "form": form},
    )


@require_http_methods(["GET", "POST"])
def manage_planning(request):
    registration = _managed_registration(request)
    if timezone.now() > settings.REGISTRATION_EDIT_DEADLINE:
        raise PermissionDenied("La date limite de modification est dépassée.")
    filter_form, sessions, form, context = _planning_context(
        request,
        registration,
        action_url=reverse("registration-manage-planning"),
    )
    if request.method == "POST" and form.is_valid():
        requests = _merge_reservation_requests(registration, sessions, form.submitted_counts())
        warnings, needs_confirmation = _over_capacity_needs_confirmation(
            request, registration, requests
        )
        if needs_confirmation:
            context["over_capacity_warnings"] = warnings
            context["management_mode"] = True
            context["session_rows"] = list(zip(sessions, form, strict=True))
            return render(request, "inscriptions/planning.html", context)
        try:
            registration = update_registration(registration, reservation_requests=requests)
            _schedule_email_with_rotated_link(request, registration, EmailLog.Kind.MODIFICATION)
        except (RegistrationError, CapacityError) as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "Le planning et le lien sécurisé ont été actualisés.")
            return redirect("registration-manage")
    context["management_mode"] = True
    context["session_rows"] = list(zip(sessions, form, strict=True))
    return render(request, "inscriptions/planning.html", context)


@require_http_methods(["GET", "POST"])
def manage_cancellation(request):
    registration = _managed_registration(request)
    if timezone.now() > settings.REGISTRATION_EDIT_DEADLINE:
        raise PermissionDenied("La date limite d’annulation est dépassée.")
    form = CancellationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            registration = cancel_registration(registration)
            schedule_registration_email(registration, EmailLog.Kind.CANCELLATION)
        except RegistrationError as error:
            form.add_error(None, str(error))
        else:
            _remember_reference(request, COMPLETED_SESSION_KEY, registration.reference)
            request.session.pop(MANAGED_SESSION_KEY, None)
            messages.success(request, "L’inscription est annulée et les places sont libérées.")
            return redirect("registration-complete", reference=registration.reference)
    return render(
        request,
        "inscriptions/cancel.html",
        {"registration": registration, "form": form},
    )
