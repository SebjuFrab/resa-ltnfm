import re
from email.mime.image import MIMEImage
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from inscriptions.models import RegistrationEvent

from .models import EmailLog

SUBJECTS = {
    EmailLog.Kind.CONFIRMATION: "Confirmation de votre inscription — La Terre est Notre Métier",
    EmailLog.Kind.MODIFICATION: "Modification de votre inscription — La Terre est Notre Métier",
    EmailLog.Kind.CANCELLATION: "Annulation de votre inscription — La Terre est Notre Métier",
}

TEMPLATE_NAMES = {
    EmailLog.Kind.CONFIRMATION: "confirmation",
    EmailLog.Kind.MODIFICATION: "modification",
    EmailLog.Kind.CANCELLATION: "cancellation",
}

EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
CONFIRMATION_LOGO_CID = "ltnfm-logo"


def _safe_error_summary(error, recipient, edit_url=""):
    """Return a short diagnostic without email addresses or line breaks."""
    summary = str(error).replace(recipient, "[destinataire]")
    if settings.EMAIL_HOST_PASSWORD:
        summary = summary.replace(settings.EMAIL_HOST_PASSWORD, "[secret]")
    if edit_url:
        summary = summary.replace(edit_url, "[lien sécurisé]")
        token = urlsplit(edit_url).fragment
        if token:
            summary = summary.replace(token, "[jeton]")
    summary = EMAIL_PATTERN.sub("[courriel]", summary)
    summary = " ".join(summary.split())
    return summary[:1000]


def _active_reservations(registration):
    return (
        registration.reservations.filter(status="ACTIVE")
        .select_related("session", "session__animation")
        .order_by("session__date", "session__starts_at", "session__animation__title")
    )


def _record_delivery_event(email_log):
    event_type = (
        RegistrationEvent.Type.EMAIL_SENT
        if email_log.status == EmailLog.Status.SENT
        else RegistrationEvent.Type.EMAIL_FAILED
    )
    RegistrationEvent.objects.create(
        registration=email_log.registration,
        event_type=event_type,
        actor_kind=RegistrationEvent.ActorKind.SYSTEM,
        changes={"email_log_id": email_log.pk, "kind": email_log.kind},
    )


def _attach_confirmation_logo(message):
    logo_path = settings.BASE_DIR / "static" / "images" / "logo-ltnfm-2020.jpg"
    with logo_path.open("rb") as logo_file:
        logo = MIMEImage(logo_file.read(), _subtype="jpeg")
    logo.add_header("Content-ID", f"<{CONFIRMATION_LOGO_CID}>")
    logo.add_header(
        "Content-Disposition",
        "inline",
        filename="la-terre-est-notre-metier.jpg",
    )
    message.mixed_subtype = "related"
    message.attach(logo)


@sensitive_variables("edit_url")
def send_registration_email(registration, kind, *, edit_url=""):
    """Send and log one registration email without propagating SMTP failures.

    ``edit_url`` is deliberately supplied by the caller: the raw edit token is
    never stored and therefore cannot be reconstructed by this service.
    """
    if kind not in TEMPLATE_NAMES:
        raise ValueError(f"Type de courriel inconnu : {kind}")

    recipient = registration.teacher.email
    email_log = EmailLog.objects.create(
        registration=registration,
        kind=kind,
        recipient=recipient,
        status=EmailLog.Status.PENDING,
    )
    context = {
        "registration": registration,
        "reservations": _active_reservations(registration),
        "group_code": (
            str(getattr(registration, "group_code", "") or "").strip()
            or registration.group_name
        ),
        "total_count": registration.student_count + registration.chaperone_count,
        "contact_email": recipient,
        "edit_url": edit_url,
        "organization_email": settings.ORGANIZATION_EMAIL,
        "organization_phone": settings.ORGANIZATION_PHONE,
        "edit_deadline": settings.REGISTRATION_EDIT_DEADLINE,
    }
    template_name = TEMPLATE_NAMES[kind]

    try:
        text_body = render_to_string(f"emails/{template_name}.txt", context)
        html_body = render_to_string(f"emails/{template_name}.html", context)
        message = EmailMultiAlternatives(
            subject=SUBJECTS[kind],
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        message.attach_alternative(html_body, "text/html")
        if kind == EmailLog.Kind.CONFIRMATION:
            _attach_confirmation_logo(message)
        sent_count = message.send(fail_silently=False)
        if sent_count != 1:
            raise RuntimeError("Le serveur de courriel n'a accepté aucun message.")
    except Exception as error:  # SMTP backends expose several exception types.
        email_log.status = EmailLog.Status.FAILED
        email_log.error_summary = _safe_error_summary(error, recipient, edit_url)
        email_log.save(update_fields=("status", "error_summary"))
    else:
        email_log.status = EmailLog.Status.SENT
        email_log.sent_at = timezone.now()
        email_log.error_summary = ""
        email_log.save(update_fields=("status", "sent_at", "error_summary"))

    _record_delivery_event(email_log)
    return email_log


def send_confirmation_email(registration, *, edit_url=""):
    return send_registration_email(
        registration, EmailLog.Kind.CONFIRMATION, edit_url=edit_url
    )


def send_modification_email(registration, *, edit_url=""):
    return send_registration_email(
        registration, EmailLog.Kind.MODIFICATION, edit_url=edit_url
    )


def send_cancellation_email(registration):
    return send_registration_email(registration, EmailLog.Kind.CANCELLATION)


@sensitive_variables("edit_url")
def schedule_registration_email(registration, kind, *, edit_url=""):
    """Schedule delivery after the surrounding database transaction commits."""
    registration_pk = registration.pk

    def deliver():
        registration_type = type(registration)
        current_registration = registration_type.objects.select_related(
            "institution", "teacher", "school_level"
        ).get(pk=registration_pk)
        send_registration_email(current_registration, kind, edit_url=edit_url)

    transaction.on_commit(deliver, robust=True)
