"""Staff bulk confirmation, processed one group per short HTTP request."""

from collections import defaultdict

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from catalogue.models import Session
from communication.mailing import mailing_sender_email
from communication.models import EmailLog
from communication.services import (
    SUBJECTS,
    _sender_contact,
    render_registration_email,
    send_registration_email,
)
from inscriptions.models import Registration, RegistrationEvent, Reservation
from inscriptions.services.capacity import CapacityError
from inscriptions.services.registration import RegistrationError, confirm_registration

from .forms import BulkConfirmationForm
from .permissions import REGISTRATION_MANAGE_PERMISSIONS

SELECTION_SALT = "operations.bulk-confirmation.selection"
SEND_SALT = "operations.bulk-confirmation.send"
MAX_AGE = 4 * 60 * 60


def _load_plan(token, user, salt):
    plan = signing.loads(token, salt=salt, max_age=MAX_AGE)
    if plan["user_id"] != user.pk:
        raise signing.BadSignature("Ce lot appartient à un autre utilisateur.")
    return plan


def _registrations():
    return (
        Registration.objects.select_related("institution", "teacher", "school_level")
        .prefetch_related(
            Prefetch(
                "reservations",
                queryset=Reservation.objects.filter(
                    status=Reservation.Status.ACTIVE
                ).select_related("session__animation"),
                to_attr="batch_reservations",
            )
        )
        .order_by("visit_date", "group_code", "pk")
    )


def _preview_issue(registration, expected_updated_at):
    if registration.status != Registration.Status.DRAFT:
        return "Ce groupe n’est plus en brouillon : aucun nouvel envoi."
    if registration.updated_at.isoformat() != expected_updated_at:
        return "Ce groupe a changé depuis l’ouverture de la page. Rechargez la liste."
    if not registration.batch_reservations:
        return "Aucune animation choisie : ce groupe restera en brouillon."
    try:
        validate_email(registration.teacher.email)
    except ValidationError:
        return "Adresse du responsable absente ou invalide : ce groupe restera en brouillon."
    return ""


@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
@require_http_methods(["GET", "POST"])
def bulk_confirmation(request):
    action = request.POST.get("action", "preview")
    if request.method == "POST":
        form = BulkConfirmationForm(request.POST, require_confirmation=action == "start")
        form.is_valid()
        try:
            plan = _load_plan(request.POST.get("selection", ""), request.user, SELECTION_SALT)
        except signing.BadSignature:
            return render(
                request,
                "operations/bulk_confirmation.html",
                {
                    "selection_error": (
                        "Cette sélection a expiré ou est invalide. Rechargez la liste."
                    ),
                },
                status=400,
            )
        registrations = list(_registrations().filter(reference__in=plan["targets"]))
    else:
        registrations = list(_registrations().filter(status=Registration.Status.DRAFT))
        plan = {
            "user_id": request.user.pk,
            "targets": {str(item.reference): item.updated_at.isoformat() for item in registrations},
        }
        form = BulkConfirmationForm(
            initial={
                "selection": signing.dumps(plan, salt=SELECTION_SALT, compress=True),
                "subject": SUBJECTS[EmailLog.Kind.CONFIRMATION],
                "message_text": (
                    "Votre groupe est bien inscrit au salon La Terre est Notre Métier. "
                    "Nous avons hâte de vous accueillir !\n\n"
                    + render_to_string("emails/includes/teacher_visit_instructions.txt").strip()
                ),
            }
        )

    ready = []
    for registration in registrations:
        registration.batch_issue = _preview_issue(
            registration, plan["targets"][str(registration.reference)]
        )
        if not registration.batch_issue:
            ready.append(registration)
    try:
        sender_email = mailing_sender_email(request.user)
        sender_error = ""
    except ValueError as error:
        sender_email = ""
        sender_error = str(error)
    if request.method == "POST" and action == "start":
        if sender_error:
            form.add_error(None, sender_error)
        if not ready:
            form.add_error(None, "Aucun brouillon prêt à être confirmé dans cette sélection.")
        if form.is_valid():
            send_plan = {
                **plan,
                "targets": {
                    str(item.reference): plan["targets"][str(item.reference)] for item in ready
                },
                "subject": form.cleaned_data["subject"],
                "message_text": form.cleaned_data["message_text"],
            }
            return render(
                request,
                "operations/bulk_confirmation_progress.html",
                {
                    "send_token": signing.dumps(send_plan, salt=SEND_SALT, compress=True),
                    "registrations": ready,
                    "excluded": [item for item in registrations if item.batch_issue],
                    "sender_email": sender_email,
                },
            )

    preview_html = ""
    example = ready[0] if ready else None
    if example and (not form.is_bound or form.is_valid()):
        _, _, preview_html = render_registration_email(
            example,
            EmailLog.Kind.CONFIRMATION,
            sender_contact=_sender_contact(request.user),
            subject=form["subject"].value(),
            message_text=form["message_text"].value(),
        )
        preview_html = preview_html.replace("cid:ltnfm-logo", static("images/logo-ltnfm-2020.jpg"))

    at = timezone.now()
    expired_holds = defaultdict(int)
    session_ids = set()
    for item in ready:
        for reservation in item.batch_reservations:
            session_ids.add(reservation.session_id)
            if item.draft_expires_at and item.draft_expires_at <= at:
                expired_holds[reservation.session_id] += reservation.total_participant_count
    over_capacity = []
    for session in (
        Session.objects.with_capacities(at=at)
        .filter(pk__in=session_ids).select_related("animation")
    ):
        session.projected_capacity = session.reserved_capacity + expired_holds[session.pk]
        if session.projected_capacity > session.max_capacity:
            over_capacity.append(session)
    return render(
        request,
        "operations/bulk_confirmation.html",
        {
            "form": form,
            "registrations": registrations,
            "ready_count": len(ready),
            "excluded_count": len(registrations) - len(ready),
            "sender_email": sender_email,
            "sender_error": sender_error,
            "preview_html": preview_html,
            "example": example,
            "over_capacity": over_capacity,
        },
    )


@transaction.non_atomic_requests
@staff_member_required
@permission_required(REGISTRATION_MANAGE_PERMISSIONS, raise_exception=True)
@require_POST
def bulk_confirmation_send(request):
    """Lock/confirm once, then send after commit; retries never send twice."""
    try:
        plan = _load_plan(request.POST.get("token", ""), request.user, SEND_SALT)
    except signing.BadSignature:
        return JsonResponse(
            {"error": "Le lot a expiré. Retournez à la liste des brouillons."}, status=400
        )
    reference = request.POST.get("reference", "")
    if reference not in plan["targets"]:
        return JsonResponse({"error": "Ce groupe ne fait pas partie du lot validé."}, status=400)
    try:
        sender_email = mailing_sender_email(request.user)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)

    try:
        with transaction.atomic():
            # Do not lock nullable joins (PostgreSQL rejects FOR UPDATE on those).
            registration = Registration.objects.select_for_update().get(reference=reference)
            if registration.status != Registration.Status.DRAFT:
                return JsonResponse(
                    {
                        "status": "skipped",
                        "message": "Déjà traité ou annulé : aucun nouvel envoi. Vérifiez la fiche.",
                    }
                )
            if registration.updated_at.isoformat() != plan["targets"][reference]:
                return JsonResponse(
                    {
                        "status": "skipped",
                        "message": (
                            "Modifié depuis l’aperçu : non validé. "
                            "Rechargez la liste pour le vérifier."
                        ),
                    }
                )
            validate_email(registration.teacher.email)
            registration = confirm_registration(
                registration,
                actor_kind=RegistrationEvent.ActorKind.STAFF,
                actor_user=request.user,
            )
    except Registration.DoesNotExist:
        return JsonResponse({"status": "skipped", "message": "Groupe introuvable."})
    except ValidationError:
        return JsonResponse(
            {
                "status": "skipped",
                "message": ("Adresse du responsable absente ou invalide : non validé."),
            }
        )
    except (RegistrationError, CapacityError) as error:
        return JsonResponse({"status": "skipped", "message": str(error)})

    email_log = send_registration_email(
        registration,
        EmailLog.Kind.CONFIRMATION,
        cc_email=sender_email,
        sender_contact=_sender_contact(request.user),
        subject=plan["subject"],
        message_text=plan["message_text"],
    )
    if email_log.status == EmailLog.Status.SENT:
        return JsonResponse({"status": "sent", "message": "Validé — mail envoyé, admin en copie."})
    return JsonResponse(
        {
            "status": "failed",
            "message": (
                "Inscription validée, mais échec de l’envoi. "
                "Consultez la fiche avant de renvoyer le mail."
            ),
        }
    )
