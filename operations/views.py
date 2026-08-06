import csv
import uuid
from collections import defaultdict
from copy import copy
from uuid import UUID

from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods, require_POST

from catalogue.models import Animation, SchoolLevel, Session, Theme
from communication.mailing import (
    MAILING_TEMPLATE_VARIABLES,
    create_and_send_mailing,
    preview_mailing_recipients,
)
from communication.models import EmailLog, MailingCampaign, MailingDelivery
from communication.rich_text import sanitize_rich_html
from communication.services import schedule_registration_email
from inscriptions.codes import generate_unique_group_code
from inscriptions.forms import CancellationForm, ConfirmationForm
from inscriptions.models import (
    GroupFamily,
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
    Teacher,
)
from inscriptions.services.capacity import CapacityError
from inscriptions.services.registration import (
    RegistrationError,
    ReservationRequest,
    cancel_registration,
    confirm_registration,
    create_draft,
    save_draft,
    update_registration,
)

from .exports import registrations_csv, reservations_csv, sessions_csv
from .forms import (
    AnimationFilterForm,
    ExportForm,
    GroupImportForm,
    MailingForm,
    RegistrationSearchForm,
    SessionImportForm,
    StaffPlanningForm,
    StaffRegistrationForm,
)
from .group_imports import (
    GROUP_IMPORT_COLUMNS,
    GroupImportError,
    import_group_payload,
    preview_group_csv,
)
from .imports import SessionImportError, import_session_payload, preview_session_csv
from .permissions import (
    REGISTRATION_DETAIL_PERMISSIONS,
    REGISTRATION_MANAGE_PERMISSIONS,
)

IMPORT_SESSION_KEY = "operations_session_import_preview"
GROUP_IMPORT_SESSION_KEY = "operations_group_import_preview"
LAST_GROUP_IMPORT_SESSION_KEY = "operations_last_group_import"
PENDING_REGISTRATION_UPDATE_KEY = "operations_pending_registration_updates"
PERSONAL_DATA_PERMISSIONS = (
    "inscriptions.view_institution",
    "inscriptions.view_teacher",
    "inscriptions.view_registration",
)
DASHBOARD_PERMISSIONS = ("catalogue.view_session", *PERSONAL_DATA_PERMISSIONS)
RESERVATION_EXPORT_PERMISSIONS = (
    "catalogue.view_session",
    "inscriptions.view_reservation",
    *PERSONAL_DATA_PERMISSIONS,
)

SESSION_IMPORT_COLUMNS = (
    "titre_animation",
    "categorie",
    "thematiques",
    "lieu_de_rendez_vous",
    "duree",
    "jauge",
    "jour",
    "horaires",
    "responsable",
    "email_responsable",
)
def _requested_date(request):
    value = request.GET.get("date", "")
    return parse_date(value) if value else None


def _requested_delimiter(request):
    return "," if request.GET.get("delimiter") == "comma" else ";"


def _filter_registrations(queryset, search_form):
    if not search_form.is_valid():
        return queryset

    query = search_form.cleaned_data["q"].strip()
    if query:
        filters = Q(institution__name__icontains=query) | Q(
            group_name__icontains=query
        )
        filters |= Q(group_code__icontains=query)
        filters |= Q(teacher__first_name__icontains=query)
        filters |= Q(teacher__last_name__icontains=query)
        filters |= Q(teacher__email__icontains=query)
        try:
            filters |= Q(reference=UUID(query))
        except ValueError:
            pass
        queryset = queryset.filter(filters)
    if search_form.cleaned_data["date"]:
        queryset = queryset.filter(visit_date=search_form.cleaned_data["date"])
    if search_form.cleaned_data["status"]:
        queryset = queryset.filter(status=search_form.cleaned_data["status"])
    return queryset


@staff_member_required
@permission_required(DASHBOARD_PERMISSIONS, raise_exception=True)
def dashboard(request):
    confirmed = Registration.objects.filter(status=Registration.Status.CONFIRMED)
    students_by_day = list(
        confirmed.values("visit_date")
        .annotate(
            student_count=Coalesce(Sum("student_count"), 0),
            chaperone_count=Coalesce(Sum("chaperone_count"), 0),
        )
        .order_by("visit_date")
    )
    for item in students_by_day:
        item["participant_count"] = item["student_count"] + item["chaperone_count"]
    unassigned = confirmed.annotate(
        active_reservation_count=Count(
            "reservations",
            filter=Q(reservations__status=Reservation.Status.ACTIVE),
        )
    ).filter(active_reservation_count=0)
    unassigned_totals = unassigned.aggregate(
        students=Coalesce(Sum("student_count"), 0),
        chaperones=Coalesce(Sum("chaperone_count"), 0),
    )
    unassigned_participants = (
        unassigned_totals["students"] + unassigned_totals["chaperones"]
    )

    session_metrics = []
    available_by_slot = defaultdict(int)
    total_capacity = 0
    total_reserved = 0
    for session in Session.objects.with_capacities(at=timezone.now()).select_related(
        "animation"
    ).order_by("date", "starts_at", "animation__title"):
        fill_rate = (
            round((session.reserved_capacity / session.max_capacity) * 100, 1)
            if session.max_capacity
            else 0
        )
        is_open = session.status == Session.Status.OPEN
        session_metrics.append(
            {
                "session": session,
                "reserved": session.reserved_capacity,
                "remaining": session.remaining_capacity,
                "fill_rate": fill_rate,
                "is_full": is_open and session.remaining_capacity == 0,
                "is_low": is_open and fill_rate < 25,
                "is_overbooked": session.reserved_capacity > session.max_capacity,
            }
        )
        if session.status == Session.Status.OPEN:
            available_by_slot[(session.date, session.starts_at)] += session.remaining_capacity
            total_capacity += session.max_capacity
            total_reserved += session.reserved_capacity

    search_form = RegistrationSearchForm(request.GET or None)
    registrations = Registration.objects.select_related(
        "institution", "teacher", "school_level"
    ).order_by("-created_at")
    registrations = _filter_registrations(registrations, search_form)

    page = Paginator(registrations, 50).get_page(request.GET.get("page"))
    for registration in page.object_list:
        try:
            registration.staff_detail_url = reverse(
                "operations:registration-detail", args=(registration.reference,)
            )
        except NoReverseMatch:
            registration.staff_detail_url = reverse("admin:index")
    pagination_query = request.GET.copy()
    pagination_query.pop("page", None)
    context = {
        "institution_count": confirmed.values("institution_id").distinct().count(),
        "group_count": confirmed.count(),
        "students_by_day": students_by_day,
        "unassigned_participants": unassigned_participants,
        "global_fill_rate": round(total_reserved / total_capacity * 100, 1)
        if total_capacity
        else 0,
        "full_session_count": sum(metric["is_full"] for metric in session_metrics),
        "low_session_count": sum(metric["is_low"] for metric in session_metrics),
        "overbooked_session_count": sum(
            metric["is_overbooked"] for metric in session_metrics
        ),
        "incomplete_count": Registration.objects.filter(
            status=Registration.Status.DRAFT
        ).count(),
        "session_metrics": session_metrics,
        "available_by_slot": [
            {"date": key[0], "starts_at": key[1], "remaining": value}
            for key, value in sorted(available_by_slot.items())
        ],
        "search_form": search_form,
        "export_form": ExportForm(),
        "registration_page": page,
        "pagination_prefix": f"{pagination_query.urlencode()}&" if pagination_query else "",
    }
    return render(request, "operations/dashboard.html", context)


@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
def registration_list(request):
    filter_form = RegistrationSearchForm(request.GET or None)
    registrations = (
        Registration.objects.select_related(
            "institution",
            "teacher",
            "family",
            "school_level",
        )
        .annotate(
            active_reservation_count=Count(
                "reservations",
                filter=Q(reservations__status=Reservation.Status.ACTIVE),
            ),
            status_order=Case(
                When(status=Registration.Status.DRAFT, then=Value(0)),
                When(status=Registration.Status.CONFIRMED, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            ),
        )
        .order_by("status_order", "-created_at", "-pk")
    )
    registrations = _filter_registrations(registrations, filter_form)
    page = Paginator(registrations, 50).get_page(request.GET.get("page"))
    last_import_references = set(
        request.session.get(LAST_GROUP_IMPORT_SESSION_KEY, ())
    )
    for registration in page.object_list:
        registration.is_from_last_import = (
            str(registration.reference) in last_import_references
        )
    pagination_query = request.GET.copy()
    pagination_query.pop("page", None)
    return render(
        request,
        "operations/registration_list.html",
        {
            "filter_form": filter_form,
            "registration_page": page,
            "draft_count": Registration.objects.filter(
                status=Registration.Status.DRAFT
            ).count(),
            "pagination_prefix": (
                f"{pagination_query.urlencode()}&" if pagination_query else ""
            ),
        },
    )


@staff_member_required
@permission_required(
    (
        "catalogue.add_animation",
        "catalogue.change_animation",
        "catalogue.add_session",
    ),
    raise_exception=True,
)
def session_import(request):
    if request.method == "POST" and request.POST.get("action") == "cancel":
        request.session.pop(IMPORT_SESSION_KEY, None)
        messages.info(request, "L'import a été abandonné.")
        return redirect("operations:session-import")

    if request.method == "POST" and request.POST.get("action") == "confirm":
        payload = request.session.get(IMPORT_SESSION_KEY)
        if not payload:
            messages.error(request, "L'aperçu a expiré. Importez de nouveau le fichier.")
            return redirect("operations:session-import")
        existing_animation_ids = {
            row.get("animation_id")
            for row in payload
            if isinstance(row, dict) and row.get("animation_id") is not None
        }
        try:
            sessions = import_session_payload(payload)
        except SessionImportError as error:
            request.session.pop(IMPORT_SESSION_KEY, None)
            return render(
                request,
                "operations/session_import.html",
                {
                    "form": SessionImportForm(),
                    "issues": error.issues,
                    "venue_categories": Animation.VenueCategory.choices,
                    "active_themes": Theme.objects.filter(is_active=True).order_by(
                        "sort_order", "name"
                    ),
                },
                status=400,
            )
        request.session.pop(IMPORT_SESSION_KEY, None)
        LogEntry.objects.log_actions(
            request.user.pk,
            sessions,
            ADDITION,
            "Séance créée par import CSV validé.",
        )
        animations = {session.animation_id: session.animation for session in sessions}
        created_animations = [
            animation
            for animation_id, animation in animations.items()
            if animation_id not in existing_animation_ids
        ]
        updated_animations = [
            animation
            for animation_id, animation in animations.items()
            if animation_id in existing_animation_ids
        ]
        if created_animations:
            LogEntry.objects.log_actions(
                request.user.pk,
                created_animations,
                ADDITION,
                "Animation créée par import CSV validé.",
            )
        if updated_animations:
            LogEntry.objects.log_actions(
                request.user.pk,
                updated_animations,
                CHANGE,
                "Catégorie et thématiques vérifiées par import CSV validé.",
            )
        messages.success(
            request,
            (
                f"{len(sessions)} créneau(x) importé(s). "
                "Les animations ont été créées ou reclassées selon le fichier."
            ),
        )
        return redirect("operations:session-import")

    form = SessionImportForm(request.POST or None, request.FILES or None)
    preview = None
    if request.method == "POST" and form.is_valid():
        preview = preview_session_csv(form.cleaned_data["file"])
        if preview.is_valid:
            request.session[IMPORT_SESSION_KEY] = [row.as_payload() for row in preview.rows]
        else:
            request.session.pop(IMPORT_SESSION_KEY, None)
    return render(
        request,
        "operations/session_import.html",
        {
            "form": form,
            "preview": preview,
            "issues": preview.issues if preview else (),
            "venue_categories": Animation.VenueCategory.choices,
            "active_themes": Theme.objects.filter(is_active=True).order_by(
                "sort_order", "name"
            ),
        },
        status=400 if preview and preview.issues else 200,
    )


@staff_member_required
@permission_required(
    (
        "catalogue.add_animation",
        "catalogue.change_animation",
        "catalogue.add_session",
    ),
    raise_exception=True,
)
def session_import_template(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        'attachment; filename="modele_import_animations.csv"'
    )
    response.write("\ufeff")

    writer = csv.writer(response, delimiter=";", lineterminator="\r\n")
    writer.writerow(SESSION_IMPORT_COLUMNS)
    writer.writerow(
        (
            "La vie du sol",
            "Extérieur",
            "Sol|Biodiversité",
            "Pôle sols",
            "45 min",
            30,
            "mercredi",
            "09:00, 10:15, 14:00",
            "Marie Martin",
            "marie.martin@example.org",
        )
    )
    writer.writerow(
        (
            "Agriculture et climat",
            "Salle",
            "Climat|Conférence",
            "Salle A",
            "1h",
            25,
            "jeudi",
            "10:00, 13:30",
            "Jean Dupont",
            "jean.dupont@example.org",
        )
    )
    return response


@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
def group_import(request):
    if request.method == "POST" and request.POST.get("action") == "cancel":
        request.session.pop(GROUP_IMPORT_SESSION_KEY, None)
        messages.info(request, "L'import des groupes a été abandonné.")
        return redirect("operations:group-import")

    if request.method == "POST" and request.POST.get("action") == "confirm":
        payload = request.session.get(GROUP_IMPORT_SESSION_KEY)
        if not payload:
            messages.error(request, "L'aperçu a expiré. Importez de nouveau le fichier.")
            return redirect("operations:group-import")
        try:
            registrations = import_group_payload(payload, actor_user=request.user)
        except GroupImportError as error:
            request.session.pop(GROUP_IMPORT_SESSION_KEY, None)
            return render(
                request,
                "operations/group_import.html",
                {"form": GroupImportForm(), "issues": error.issues},
                status=400,
        )
        request.session.pop(GROUP_IMPORT_SESSION_KEY, None)
        request.session[LAST_GROUP_IMPORT_SESSION_KEY] = [
            str(registration.reference) for registration in registrations
        ]
        count = len(registrations)
        imported_label = (
            "groupe importé comme brouillon"
            if count == 1
            else "groupes importés comme brouillons"
        )
        messages.success(
            request,
            (
                f"{count} {imported_label}. "
                "Étape suivante : attribuer les animations et confirmer chaque inscription. "
                "Aucun courriel n'a été envoyé."
            ),
        )
        return redirect(
            f"{reverse('operations:registration-list')}"
            f"?status={Registration.Status.DRAFT}"
        )

    form = GroupImportForm(request.POST or None, request.FILES or None)
    preview = None
    if request.method == "POST" and form.is_valid():
        preview = preview_group_csv(form.cleaned_data["file"])
        if preview.is_valid:
            request.session[GROUP_IMPORT_SESSION_KEY] = [
                row.as_payload() for row in preview.rows
            ]
        else:
            request.session.pop(GROUP_IMPORT_SESSION_KEY, None)
    return render(
        request,
        "operations/group_import.html",
        {
            "form": form,
            "preview": preview,
            "issues": preview.issues if preview else (),
        },
        status=400 if preview and preview.issues else 200,
    )


@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
def group_import_template(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        'attachment; filename="modele_import_groupes.csv"'
    )
    response.write("\ufeff")

    writer = csv.writer(response, delimiter=";", lineterminator="\r\n")
    writer.writerow(GROUP_IMPORT_COLUMNS)
    writer.writerow(
        (
            "Martin",
            "Marie",
            "marie.martin@example.org",
            "06 12 34 56 78",
            "Lycée agricole de Retiers",
            "AGRICULTURAL",
            "Retiers",
            "35",
            "truffe-verte",
            "lycee-agricole",
            "LYC_2DE",
            "2026-09-23",
            24,
            2,
            26,
            "Classe de seconde",
            "Arrivée prévue à 9 h",
        )
    )
    writer.writerow(
        (
            "Dupont",
            "Jean",
            "jean.dupont@example.org",
            "06 98 76 54 32",
            "Institut supérieur de Rennes",
            "HIGHER_EDUCATION",
            "Rennes",
            "35",
            "pomme-doree",
            "enseignement-superieur",
            "SUP_LICENCE_BUT",
            "2026-09-24",
            30,
            2,
            32,
            "Licence 2",
            "",
        )
    )
    return response


@staff_member_required
@permission_required(PERSONAL_DATA_PERMISSIONS, raise_exception=True)
def export_registrations(request):
    return registrations_csv(
        visit_date=_requested_date(request), delimiter=_requested_delimiter(request)
    )


@staff_member_required
@permission_required(RESERVATION_EXPORT_PERMISSIONS, raise_exception=True)
def export_reservations(request):
    return reservations_csv(
        visit_date=_requested_date(request), delimiter=_requested_delimiter(request)
    )


@staff_member_required
@permission_required(RESERVATION_EXPORT_PERMISSIONS, raise_exception=True)
def export_sessions(request):
    return sessions_csv(
        visit_date=_requested_date(request), delimiter=_requested_delimiter(request)
    )


@staff_member_required
def export_download(request):
    form = ExportForm(request.GET or None)
    if not form.is_valid():
        messages.error(request, "Choisissez un export et, si besoin, un jour valide.")
        return redirect("operations:dashboard")
    exporters = {
        ExportForm.ExportType.REGISTRATIONS: registrations_csv,
        ExportForm.ExportType.RESERVATIONS: reservations_csv,
        ExportForm.ExportType.SESSIONS: sessions_csv,
    }
    required_permissions = {
        ExportForm.ExportType.REGISTRATIONS: PERSONAL_DATA_PERMISSIONS,
        ExportForm.ExportType.RESERVATIONS: RESERVATION_EXPORT_PERMISSIONS,
        ExportForm.ExportType.SESSIONS: RESERVATION_EXPORT_PERMISSIONS,
    }
    export_type = form.cleaned_data["export_type"]
    if not request.user.has_perms(required_permissions[export_type]):
        raise PermissionDenied
    delimiter = "," if form.cleaned_data["delimiter"] == "comma" else ";"
    return exporters[export_type](
        visit_date=form.cleaned_data["date"], delimiter=delimiter
    )


def _staff_registration(reference):
    return get_object_or_404(
        Registration.objects.select_related(
            "institution", "teacher", "school_level", "family"
        ),
        reference=reference,
    )


def _pending_update_payload(data):
    institution = data["existing_institution"]
    return {
        "existing_institution_id": institution.pk if institution else None,
        "institution_type": data["institution_type"],
        "institution_name": data["institution_name"],
        "institution_city": data["institution_city"],
        "institution_department": data["institution_department"],
        "teacher_last_name": data["teacher_last_name"],
        "teacher_first_name": data["teacher_first_name"],
        "teacher_email": data["teacher_email"],
        "teacher_phone": data["teacher_phone"],
        "group_code": data["group_code"],
        "family_id": data["family"].pk,
        "school_level_id": data["school_level"].pk,
        "visit_date": data["visit_date"].isoformat(),
        "student_count": data["student_count"],
        "chaperone_count": data["chaperone_count"],
        "level_comment": data["level_comment"],
        "comment": data["comment"],
    }


def _store_pending_update(request, registration, data):
    pending_updates = request.session.get(PENDING_REGISTRATION_UPDATE_KEY, {})
    pending_updates[str(registration.reference)] = _pending_update_payload(data)
    request.session[PENDING_REGISTRATION_UPDATE_KEY] = pending_updates


def _clear_pending_update(request, registration):
    pending_updates = request.session.get(PENDING_REGISTRATION_UPDATE_KEY, {})
    if pending_updates.pop(str(registration.reference), None) is not None:
        if pending_updates:
            request.session[PENDING_REGISTRATION_UPDATE_KEY] = pending_updates
        else:
            request.session.pop(PENDING_REGISTRATION_UPDATE_KEY, None)
        request.session.modified = True


def _load_pending_update(request, registration):
    payload = request.session.get(PENDING_REGISTRATION_UPDATE_KEY, {}).get(
        str(registration.reference)
    )
    if not payload:
        return None
    try:
        existing_institution = (
            Institution.objects.get(pk=payload["existing_institution_id"])
            if payload["existing_institution_id"]
            else None
        )
        family = GroupFamily.objects.get(pk=payload["family_id"])
        school_level = SchoolLevel.objects.get(pk=payload["school_level_id"])
    except (Institution.DoesNotExist, GroupFamily.DoesNotExist, SchoolLevel.DoesNotExist):
        _clear_pending_update(request, registration)
        return None
    return {
        "existing_institution": existing_institution,
        "institution_type": payload["institution_type"],
        "institution_name": payload["institution_name"],
        "institution_city": payload["institution_city"],
        "institution_department": payload["institution_department"],
        "teacher_last_name": payload["teacher_last_name"],
        "teacher_first_name": payload["teacher_first_name"],
        "teacher_email": payload["teacher_email"],
        "teacher_phone": payload["teacher_phone"],
        "group_code": payload["group_code"],
        "family": family,
        "school_level": school_level,
        "visit_date": parse_date(payload["visit_date"]),
        "student_count": payload["student_count"],
        "chaperone_count": payload["chaperone_count"],
        "level_comment": payload["level_comment"],
        "comment": payload["comment"],
    }


def _save_contact_details(data, *, registration=None):
    institution = data["existing_institution"]
    if institution is None:
        institution = Institution.objects.filter(
            name__iexact=data["institution_name"].strip(),
            city__iexact=data["institution_city"].strip(),
            department__iexact=data["institution_department"].strip(),
        ).first()
        if institution is None:
            institution = Institution.objects.create(
                name=data["institution_name"].strip(),
                institution_type=data["institution_type"],
                address="",
                postal_code="",
                city=data["institution_city"].strip(),
                department=data["institution_department"].strip(),
            )

    values = {
        "first_name": data["teacher_first_name"].strip(),
        "last_name": data["teacher_last_name"].strip(),
        "email": data["teacher_email"].strip(),
        "phone": data["teacher_phone"].strip(),
    }
    teacher = Teacher.objects.filter(
        institution=institution, email__iexact=values["email"]
    ).first()
    if teacher is None:
        teacher = Teacher.objects.create(institution=institution, **values)
        return institution, teacher

    changed_fields = [
        field for field, value in values.items() if getattr(teacher, field) != value
    ]
    other_registrations = teacher.registrations.all()
    if registration is not None:
        other_registrations = other_registrations.exclude(pk=registration.pk)
    if changed_fields and other_registrations.exists():
        teacher = Teacher.objects.create(institution=institution, **values)
    elif changed_fields:
        for field, value in values.items():
            setattr(teacher, field, value)
        teacher.save(update_fields=(*changed_fields, "updated_at"))
    return institution, teacher


def _active_reservations(registration):
    return registration.reservations.filter(
        status=Reservation.Status.ACTIVE
    ).select_related("session", "session__animation")


@staff_member_required
@permission_required("catalogue.view_session", raise_exception=True)
def animation_list(request):
    sessions = (
        Session.objects.with_capacities(at=timezone.now())
        .select_related("animation")
        .prefetch_related("animation__themes")
        .order_by("date", "starts_at", "animation__title")
    )
    filter_form = AnimationFilterForm(request.GET or None)
    sessions = list(filter_form.apply(sessions))
    return render(
        request,
        "operations/animation_list.html",
        {"filter_form": filter_form, "sessions": sessions},
    )


@staff_member_required
@permission_required("inscriptions.add_registration", raise_exception=True)
def group_code_suggestion(request):
    return JsonResponse({"code": generate_unique_group_code()})


@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
@require_http_methods(["GET", "POST"])
def registration_create(request):
    form = StaffRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                institution, teacher = _save_contact_details(data)
                access = create_draft(
                    institution=institution,
                    teacher=teacher,
                    group_code=data["group_code"],
                    group_name=data["group_code"],
                    family=data["family"],
                    school_level=data["school_level"],
                    student_count=data["student_count"],
                    chaperone_count=data["chaperone_count"],
                    visit_date=data["visit_date"],
                    level_comment=data["level_comment"],
                    comment=data["comment"],
                    actor_kind=RegistrationEvent.ActorKind.STAFF,
                    actor_user=request.user,
                )
        except (RegistrationError, CapacityError) as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request,
                f"Le groupe {access.registration.group_code} est créé. Choisissez ses animations.",
            )
            return redirect(
                "operations:registration-planning",
                reference=access.registration.reference,
            )
    return render(request, "operations/registration_form.html", {"form": form})


def _planning_context(request, registration, *, pending_update=None):
    planning_registration = copy(registration)
    institution_name = registration.institution.name
    if pending_update:
        planning_registration.group_code = pending_update["group_code"]
        planning_registration.student_count = pending_update["student_count"]
        planning_registration.chaperone_count = pending_update["chaperone_count"]
        planning_registration.visit_date = pending_update["visit_date"]
        institution_name = (
            pending_update["existing_institution"].name
            if pending_update["existing_institution"]
            else pending_update["institution_name"]
        )
    filter_form = AnimationFilterForm(
        request.GET or None,
        default_date=planning_registration.visit_date,
    )
    planning_date = filter_form.selected_date
    planning_registration.visit_date = planning_date
    pending_day_change = planning_date != registration.visit_date
    capacity_at = timezone.now()
    current_session_ids = set(
        registration.reservations.filter(
            status=Reservation.Status.ACTIVE,
            session__date=planning_date,
        ).values_list("session_id", flat=True)
    )
    session_queryset = (
        Session.objects.with_capacities(at=capacity_at)
        .filter(date=planning_date)
        .filter(Q(animation__is_active=True) | Q(pk__in=current_session_ids))
        .select_related("animation")
        .prefetch_related("animation__recommended_levels", "animation__themes")
        .order_by("starts_at", "ends_at", "animation__title")
    )
    all_sessions = list(session_queryset)
    sessions = list(filter_form.apply(session_queryset))
    displayed_ids = {session.pk for session in sessions}
    unavailable_reservations = [
        session
        for session in all_sessions
        if session.pk in current_session_ids
        and (
            not session.animation.is_active
            or session.status == Session.Status.CANCELLED
        )
    ]
    sessions.extend(
        session
        for session in unavailable_reservations
        if session.pk not in displayed_ids
    )
    sessions.sort(
        key=lambda session: (
            session.starts_at,
            session.ends_at,
            session.animation.title,
        )
    )
    planning_form = StaffPlanningForm(
        request.POST or None,
        registration=planning_registration,
        sessions=all_sessions,
        capacity_at=capacity_at,
    )
    action_url = reverse(
        "operations:registration-planning",
        kwargs={"reference": registration.reference},
    )
    planning_post_query = f"date={planning_date.isoformat()}"
    if pending_update:
        planning_post_query = f"reschedule=1&{planning_post_query}"
    return sessions, planning_form, {
        "registration": planning_registration,
        "institution_name": institution_name,
        "is_rescheduling": bool(pending_update),
        "pending_day_change": pending_day_change,
        "planning_date": planning_date,
        "filter_form": filter_form,
        "planning_form": planning_form,
        "session_rows": planning_form.session_rows(sessions),
        "action_url": action_url,
        "planning_post_url": f"{action_url}?{planning_post_query}",
    }


def _planning_requests(
    registration,
    displayed_sessions,
    requested_counts,
    *,
    retain_hidden=True,
):
    displayed_ids = {session.pk for session in displayed_sessions}
    retained_counts = {}
    if retain_hidden:
        retained_counts = {
            reservation.session_id: (
                reservation.student_count,
                reservation.chaperone_count,
            )
            for reservation in registration.reservations.filter(
                status=Reservation.Status.ACTIVE
            )
            if reservation.session_id not in displayed_ids
        }
    retained_counts.update(requested_counts)
    return [
        ReservationRequest(
            session_id=session_id,
            student_count=student_count,
            chaperone_count=chaperone_count,
        )
        for session_id, (student_count, chaperone_count) in sorted(
            retained_counts.items()
        )
    ]


@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
@require_http_methods(["GET", "POST"])
def registration_planning(request, reference):
    registration = _staff_registration(reference)
    if registration.status == Registration.Status.CANCELLED:
        messages.error(request, "Une inscription annulée ne peut plus être modifiée.")
        return redirect("operations:registration-detail", reference=reference)
    is_rescheduling = request.GET.get("reschedule") == "1"
    pending_update = (
        _load_pending_update(request, registration) if is_rescheduling else None
    )
    if is_rescheduling and pending_update is None:
        messages.error(
            request,
            "La modification en attente a expiré. Recommencez depuis la fiche du groupe.",
        )
        return redirect("operations:registration-update", reference=reference)
    if (
        request.method == "POST"
        and pending_update
        and request.POST.get("action") == "cancel-reschedule"
    ):
        _clear_pending_update(request, registration)
        messages.info(request, "Les modifications en attente ont été abandonnées.")
        return redirect("operations:registration-detail", reference=reference)
    sessions, form, context = _planning_context(
        request,
        registration,
        pending_update=pending_update,
    )
    filter_form = context["filter_form"]
    if request.method == "POST" and filter_form.is_bound and not filter_form.is_valid():
        form.add_error(
            None,
            "Le jour ou l’un des filtres est invalide. Corrigez les filtres avant "
            "d’enregistrer le planning.",
        )
    elif request.method == "POST" and form.is_valid():
        requested_counts = form.requested_counts()
        reservation_requests = _planning_requests(
            context["registration"],
            sessions,
            requested_counts,
            retain_hidden=not context["pending_day_change"],
        )
        if context["pending_day_change"] and not reservation_requests:
            form.add_error(
                None,
                "Sélectionnez au moins une animation avant d'enregistrer les modifications.",
            )
            context["session_rows"] = form.session_rows(sessions)
            return render(request, "operations/registration_planning.html", context)
        try:
            if pending_update:
                with transaction.atomic():
                    institution, teacher = _save_contact_details(
                        pending_update,
                        registration=registration,
                    )
                    registration = update_registration(
                        registration,
                        institution=institution,
                        teacher=teacher,
                        group_code=pending_update["group_code"],
                        group_name=pending_update["group_code"],
                        family=pending_update["family"],
                        school_level=pending_update["school_level"],
                        student_count=pending_update["student_count"],
                        chaperone_count=pending_update["chaperone_count"],
                        visit_date=context["planning_date"],
                        level_comment=pending_update["level_comment"],
                        comment=pending_update["comment"],
                        reservation_requests=reservation_requests,
                        actor_kind=RegistrationEvent.ActorKind.STAFF,
                        actor_user=request.user,
                    )
                    if registration.status == Registration.Status.CONFIRMED:
                        schedule_registration_email(
                            registration, EmailLog.Kind.MODIFICATION
                        )
            elif registration.status == Registration.Status.DRAFT:
                registration = save_draft(
                    registration,
                    visit_date=context["planning_date"],
                    reservation_requests=reservation_requests,
                    actor_kind=RegistrationEvent.ActorKind.STAFF,
                    actor_user=request.user,
                )
            else:
                registration = update_registration(
                    registration,
                    visit_date=context["planning_date"],
                    reservation_requests=reservation_requests,
                    actor_kind=RegistrationEvent.ActorKind.STAFF,
                    actor_user=request.user,
                )
                schedule_registration_email(
                    registration, EmailLog.Kind.MODIFICATION
                )
        except (RegistrationError, CapacityError) as error:
            form.add_error(None, str(error))
        else:
            if pending_update:
                _clear_pending_update(request, registration)
                if registration.status == Registration.Status.DRAFT:
                    messages.success(
                        request,
                        "Les modifications et le programme du brouillon sont enregistrés.",
                    )
                    return redirect(
                        "operations:registration-review",
                        reference=registration.reference,
                    )
                messages.success(
                    request,
                    "Les modifications et le programme sont enregistrés. "
                    "Le professeur va recevoir un récapitulatif de modification.",
                )
                return redirect(
                    "operations:registration-detail",
                    reference=registration.reference,
                )
            if registration.status == Registration.Status.DRAFT:
                return redirect(
                    "operations:registration-review", reference=registration.reference
                )
            messages.success(
                request,
                "Le programme est modifié et le professeur va recevoir un nouveau récapitulatif.",
            )
            return redirect(
                "operations:registration-detail", reference=registration.reference
            )
    context["session_rows"] = form.session_rows(sessions)
    return render(request, "operations/registration_planning.html", context)


@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
@require_http_methods(["GET", "POST"])
def registration_review(request, reference):
    registration = _staff_registration(reference)
    if registration.status != Registration.Status.DRAFT:
        return redirect("operations:registration-detail", reference=reference)
    reservations = _active_reservations(registration)
    form = ConfirmationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            registration = confirm_registration(
                registration,
                actor_kind=RegistrationEvent.ActorKind.STAFF,
                actor_user=request.user,
            )
            schedule_registration_email(registration, EmailLog.Kind.CONFIRMATION)
        except (RegistrationError, CapacityError) as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request,
                "L'inscription est confirmée et le récapitulatif va être envoyé au professeur.",
            )
            return redirect(
                "operations:registration-detail", reference=registration.reference
            )
    return render(
        request,
        "operations/registration_review.html",
        {"registration": registration, "reservations": reservations, "form": form},
    )


@staff_member_required
@permission_required(REGISTRATION_DETAIL_PERMISSIONS, raise_exception=True)
def registration_detail(request, reference):
    registration = _staff_registration(reference)
    return render(
        request,
        "operations/registration_detail.html",
        {
            "registration": registration,
            "reservations": _active_reservations(registration),
            "email_logs": registration.email_logs.order_by("-created_at")[:20],
        },
    )


@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
@require_http_methods(["GET", "POST"])
def registration_update(request, reference):
    registration = _staff_registration(reference)
    if registration.status == Registration.Status.CANCELLED:
        messages.error(request, "Une inscription annulée ne peut plus être modifiée.")
        return redirect("operations:registration-detail", reference=reference)
    if request.method == "GET":
        _clear_pending_update(request, registration)
    form = StaffRegistrationForm(
        request.POST or None, registration=registration
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        day_changed = data["visit_date"] != registration.visit_date
        participant_counts_changed = (
            data["student_count"] != registration.student_count
            or data["chaperone_count"] != registration.chaperone_count
        )
        has_active_reservations = registration.reservations.filter(
            status=Reservation.Status.ACTIVE
        ).exists()
        if day_changed or (participant_counts_changed and has_active_reservations):
            _store_pending_update(request, registration, data)
            messages.warning(
                request,
                "Vérifiez la répartition du groupe dans les animations. Rien ne sera "
                "modifié avant l'enregistrement de cette étape.",
            )
            planning_url = reverse(
                "operations:registration-planning",
                kwargs={"reference": registration.reference},
            )
            return redirect(f"{planning_url}?reschedule=1")
        try:
            with transaction.atomic():
                institution, teacher = _save_contact_details(
                    data, registration=registration
                )
                registration = update_registration(
                    registration,
                    institution=institution,
                    teacher=teacher,
                    group_code=data["group_code"],
                    group_name=data["group_code"],
                    family=data["family"],
                    school_level=data["school_level"],
                    student_count=data["student_count"],
                    chaperone_count=data["chaperone_count"],
                    visit_date=data["visit_date"],
                    level_comment=data["level_comment"],
                    comment=data["comment"],
                    actor_kind=RegistrationEvent.ActorKind.STAFF,
                    actor_user=request.user,
                )
                if registration.status == Registration.Status.CONFIRMED:
                    schedule_registration_email(
                        registration, EmailLog.Kind.MODIFICATION
                    )
        except (RegistrationError, CapacityError) as error:
            form.add_error(None, str(error))
        else:
            _clear_pending_update(request, registration)
            if registration.status == Registration.Status.DRAFT:
                messages.success(request, "La fiche du brouillon est mise à jour.")
                return redirect(
                    "operations:registration-review",
                    reference=registration.reference,
                )
            messages.success(
                request,
                "La fiche est modifiée et le professeur va recevoir un nouveau récapitulatif.",
            )
            return redirect(
                "operations:registration-detail", reference=registration.reference
            )
    return render(
        request,
        "operations/registration_form.html",
        {"form": form, "registration": registration},
    )


@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
@require_http_methods(["GET", "POST"])
def registration_cancel(request, reference):
    registration = _staff_registration(reference)
    if registration.status == Registration.Status.CANCELLED:
        messages.info(request, "Cette inscription est déjà annulée.")
        return redirect("operations:registration-detail", reference=reference)
    form = CancellationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            registration = cancel_registration(
                registration,
                actor_kind=RegistrationEvent.ActorKind.STAFF,
                actor_user=request.user,
            )
            schedule_registration_email(registration, EmailLog.Kind.CANCELLATION)
        except RegistrationError as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request, "L'inscription est annulée et le professeur va être prévenu."
            )
            return redirect(
                "operations:registration-detail", reference=registration.reference
            )
    return render(
        request,
        "operations/registration_cancel.html",
        {"registration": registration, "form": form},
    )


@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
@require_POST
def registration_resend(request, reference):
    registration = _staff_registration(reference)
    if registration.status != Registration.Status.CONFIRMED:
        messages.error(request, "Seule une inscription confirmée peut être renvoyée.")
    else:
        schedule_registration_email(registration, EmailLog.Kind.CONFIRMATION)
        messages.success(request, "Le récapitulatif va être renvoyé au professeur.")
    return redirect("operations:registration-detail", reference=reference)


@staff_member_required
@permission_required("communication.send_mailing", raise_exception=True)
@require_http_methods(["GET", "POST"])
def mailing_create(request):
    initial = {
        "subject": "Informations pratiques pour votre groupe — La Terre est Notre Métier",
        "body_html": (
            "<p>Vous trouverez ci-dessous le programme et les informations "
            "utiles pour préparer la venue de votre groupe au salon.</p>"
        ),
        "organizer_subject": (
            "Organisation de votre animation — La Terre est Notre Métier"
        ),
        "organizer_body_html": (
            "<p>Vous trouverez ci-dessous les horaires, les effectifs et les "
            "coordonnées des groupes attendus sur vos animations.</p>"
        ),
    }
    form = MailingForm(request.POST or None, initial=initial)
    preview = None
    if request.method == "GET":
        preview = preview_mailing_recipients()
    elif form.is_valid():
        data = form.cleaned_data
        preview = preview_mailing_recipients(
            visit_date=data["visit_date"], family=data["family"]
        )
        if request.POST.get("action") == "send":
            has_missing_addresses = (
                preview.missing_teacher_email_count
                or preview.missing_organizer_email_count
            )
            if has_missing_addresses and not data["confirm_missing"]:
                form.add_error(
                    "confirm_missing",
                    "Confirmez explicitement l'envoi incomplet ou corrigez les adresses.",
                )
            else:
                try:
                    result = create_and_send_mailing(
                        subject=data["subject"],
                        body_html=data["body_html"],
                        organizer_subject=data["organizer_subject"],
                        organizer_body_html=data["organizer_body_html"],
                        created_by=request.user,
                        visit_date=data["visit_date"],
                        family=data["family"],
                        idempotency_key=request.POST.get("idempotency_key") or None,
                    )
                except ValueError as error:
                    form.add_error(None, str(error))
                else:
                    if result.failed_count:
                        messages.warning(
                            request,
                            f"Publipostage terminé : {result.sent_count} envoyé(s), "
                            f"{result.failed_count} échec(s).",
                        )
                    else:
                        messages.success(
                            request,
                            f"Publipostage terminé : {result.sent_count} message(s) envoyé(s).",
                        )
                    return redirect(
                        "operations:mailing-detail", campaign_id=result.campaign.pk
                    )
    source_html = (
        request.POST.get("body_html", "")
        if request.method == "POST"
        else initial["body_html"]
    )
    organizer_source_html = (
        request.POST.get("organizer_body_html", "")
        if request.method == "POST"
        else initial["organizer_body_html"]
    )
    return render(
        request,
        "operations/mailing_form.html",
        {
            "form": form,
            "preview": preview,
            "editor_html": sanitize_rich_html(source_html),
            "organizer_editor_html": sanitize_rich_html(organizer_source_html),
            "mailing_template_variables": MAILING_TEMPLATE_VARIABLES,
            "idempotency_key": request.POST.get("idempotency_key")
            or uuid.uuid4().hex,
        },
    )


@staff_member_required
@permission_required("communication.send_mailing", raise_exception=True)
def mailing_detail(request, campaign_id):
    campaign = get_object_or_404(
        MailingCampaign.objects.select_related("created_by"), pk=campaign_id
    )
    deliveries = campaign.deliveries.order_by(
        "recipient_kind", "recipient", "pk"
    )
    group_deliveries = deliveries.filter(
        recipient_kind=MailingDelivery.RecipientKind.TEACHER
    )
    organizer_deliveries = deliveries.filter(
        recipient_kind=MailingDelivery.RecipientKind.ORGANIZER
    )
    return render(
        request,
        "operations/mailing_detail.html",
        {
            "campaign": campaign,
            "deliveries": deliveries,
            "group_sent_count": group_deliveries.filter(
                status=MailingDelivery.Status.SENT
            ).count(),
            "organizer_sent_count": organizer_deliveries.filter(
                status=MailingDelivery.Status.SENT
            ).count(),
            "sent_count": deliveries.filter(
                status=MailingDelivery.Status.SENT
            ).count(),
            "failed_count": deliveries.filter(
                status=MailingDelivery.Status.FAILED
            ).count(),
        },
    )
