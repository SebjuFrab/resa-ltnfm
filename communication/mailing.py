"""Recipient selection, personalization and delivery for final mailings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.template.loader import render_to_string
from django.utils import timezone

from inscriptions.models import Registration, Reservation

from .models import MailingCampaign, MailingDelivery
from .rich_text import rich_html_to_text, sanitize_rich_html
from .services import _safe_error_summary


@dataclass(frozen=True, slots=True)
class MailingRecipientPreview:
    teacher_count: int
    organizer_count: int
    total_count: int
    missing_teacher_email_count: int
    missing_organizer_email_count: int

    def as_dict(self):
        return {
            "teacher_count": self.teacher_count,
            "organizer_count": self.organizer_count,
            "total_count": self.total_count,
            "missing_teacher_email_count": self.missing_teacher_email_count,
            "missing_organizer_email_count": self.missing_organizer_email_count,
        }


@dataclass(frozen=True, slots=True)
class MailingSendResult:
    campaign: MailingCampaign
    sent_count: int
    failed_count: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class _RecipientSpec:
    recipient_kind: str
    recipient: str
    recipient_name: str
    dedupe_key: str
    context_snapshot: dict
    registration_id: int | None = None


def _visit_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError("Le jour de visite n’est pas valide.") from error


def _family_values(family):
    if family in (None, ""):
        return "", ""
    if getattr(family, "pk", None) is not None:
        return str(family.pk), str(family)
    return str(family).strip(), str(family).strip()


def _apply_family_filter(queryset, family):
    if family in (None, ""):
        return queryset
    try:
        Registration._meta.get_field("family")
    except FieldDoesNotExist as error:
        raise ValueError("Le filtre par famille n’est pas disponible.") from error
    if getattr(family, "pk", None) is not None:
        return queryset.filter(family=family)
    value = str(family).strip()
    if value.isdigit():
        return queryset.filter(family_id=int(value))
    return queryset.filter(Q(family__slug__iexact=value) | Q(family__name__iexact=value))


def _normalized_email(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        validate_email(value)
    except ValidationError:
        return ""
    local, domain = value.rsplit("@", 1)
    return f"{local}@{domain.lower()}"


def _group_code(registration):
    return (
        str(getattr(registration, "group_code", "") or "").strip()
        or registration.group_name
        or str(registration.reference)
    )


def _total_count(student_count, chaperone_count):
    return int(student_count or 0) + int(chaperone_count or 0)


def _registration_snapshot(registration):
    reservations = []
    for reservation in registration.mailing_reservations:
        session = reservation.session
        reservations.append(
            {
                "date": session.date.isoformat(),
                "date_label": session.date.strftime("%d/%m/%Y"),
                "starts_at": session.starts_at.strftime("%H:%M"),
                "ends_at": session.ends_at.strftime("%H:%M"),
                "animation": session.animation.title,
                "location": session.location,
                "student_count": reservation.student_count,
                "chaperone_count": reservation.chaperone_count,
                "total_count": _total_count(
                    reservation.student_count, reservation.chaperone_count
                ),
            }
        )
    family = getattr(registration, "family", None)
    return {
        "group_code": _group_code(registration),
        "group_name": registration.group_name,
        "institution": registration.institution.name,
        "visit_date": registration.visit_date.isoformat(),
        "visit_date_label": registration.visit_date.strftime("%d/%m/%Y"),
        "family": str(family) if family else "",
        "school_level": str(registration.school_level),
        "student_count": registration.student_count,
        "chaperone_count": registration.chaperone_count,
        "total_count": _total_count(
            registration.student_count, registration.chaperone_count
        ),
        "teacher_first_name": registration.teacher.first_name,
        "teacher_last_name": registration.teacher.last_name,
        "teacher_email": registration.teacher.email,
        "reservations": reservations,
    }


def _registration_queryset(*, visit_date=None, family=None):
    related = ["institution", "teacher", "school_level"]
    try:
        Registration._meta.get_field("family")
    except FieldDoesNotExist:
        pass
    else:
        related.append("family")
    reservation_queryset = (
        Reservation.objects.filter(status=Reservation.Status.ACTIVE)
        .select_related("session", "session__animation")
        .order_by("session__date", "session__starts_at", "session__animation__title")
    )
    queryset = Registration.objects.filter(
        status=Registration.Status.CONFIRMED,
        anonymized_at__isnull=True,
    )
    if visit_date := _visit_date(visit_date):
        queryset = queryset.filter(visit_date=visit_date)
    else:
        queryset = queryset.filter(
            visit_date__in=[date.fromisoformat(value) for value in settings.EVENT_DATES]
        )
    queryset = _apply_family_filter(queryset, family)
    return (
        queryset.select_related(*related)
        .prefetch_related(
            Prefetch(
                "reservations",
                queryset=reservation_queryset,
                to_attr="mailing_reservations",
            )
        )
        .order_by("visit_date", "institution__name", "pk")
    )


def _recipient_specs(*, visit_date=None, family=None):
    registrations = list(
        _registration_queryset(visit_date=visit_date, family=family)
    )
    specs = []
    missing_teacher_email_count = 0
    missing_organizer_session_ids = set()
    organizer_buckets = {}

    for registration in registrations:
        snapshot = _registration_snapshot(registration)
        teacher_email = _normalized_email(registration.teacher.email)
        if teacher_email:
            teacher_name = " ".join(
                part
                for part in (
                    registration.teacher.first_name,
                    registration.teacher.last_name,
                )
                if part
            )
            specs.append(
                _RecipientSpec(
                    recipient_kind=MailingDelivery.RecipientKind.TEACHER,
                    recipient=teacher_email,
                    recipient_name=teacher_name,
                    dedupe_key=f"teacher:registration:{registration.pk}",
                    context_snapshot={"registration": snapshot},
                    registration_id=registration.pk,
                )
            )
        else:
            missing_teacher_email_count += 1

        for reservation in registration.mailing_reservations:
            session = reservation.session
            organizer_email = _normalized_email(
                getattr(session, "organizer_email", "")
            )
            if not organizer_email:
                missing_organizer_session_ids.add(session.pk)
                continue
            bucket_key = organizer_email.casefold()
            bucket = organizer_buckets.setdefault(
                bucket_key,
                {
                    "recipient": organizer_email,
                    "names": set(),
                    "sessions": {},
                },
            )
            organizer_name = str(getattr(session, "organizer", "") or "").strip()
            if organizer_name:
                bucket["names"].add(organizer_name)
            session_snapshot = bucket["sessions"].setdefault(
                session.pk,
                {
                    "date": session.date.isoformat(),
                    "date_label": session.date.strftime("%d/%m/%Y"),
                    "starts_at": session.starts_at.strftime("%H:%M"),
                    "ends_at": session.ends_at.strftime("%H:%M"),
                    "animation": session.animation.title,
                    "location": session.location,
                    "groups": [],
                    "total_count": 0,
                },
            )
            group_snapshot = {
                "registration_id": registration.pk,
                "group_code": snapshot["group_code"],
                "group_name": snapshot["group_name"],
                "institution": snapshot["institution"],
                "teacher_name": " ".join(
                    part
                    for part in (
                        snapshot["teacher_first_name"],
                        snapshot["teacher_last_name"],
                    )
                    if part
                ),
                "teacher_email": snapshot["teacher_email"],
                "student_count": reservation.student_count,
                "chaperone_count": reservation.chaperone_count,
                "total_count": _total_count(
                    reservation.student_count, reservation.chaperone_count
                ),
            }
            session_snapshot["groups"].append(group_snapshot)
            session_snapshot["total_count"] += group_snapshot["total_count"]

    for bucket_key, bucket in sorted(organizer_buckets.items()):
        sessions = sorted(
            bucket["sessions"].values(),
            key=lambda item: (item["date"], item["starts_at"], item["animation"]),
        )
        for session in sessions:
            session["groups"].sort(
                key=lambda group: (
                    group["institution"].casefold(),
                    group["group_code"].casefold(),
                )
            )
        specs.append(
            _RecipientSpec(
                recipient_kind=MailingDelivery.RecipientKind.ORGANIZER,
                recipient=bucket["recipient"],
                recipient_name=", ".join(sorted(bucket["names"])),
                dedupe_key=f"organizer:{bucket_key}",
                context_snapshot={"sessions": sessions},
            )
        )

    teacher_count = sum(
        spec.recipient_kind == MailingDelivery.RecipientKind.TEACHER for spec in specs
    )
    organizer_count = sum(
        spec.recipient_kind == MailingDelivery.RecipientKind.ORGANIZER for spec in specs
    )
    preview = MailingRecipientPreview(
        teacher_count=teacher_count,
        organizer_count=organizer_count,
        total_count=len(specs),
        missing_teacher_email_count=missing_teacher_email_count,
        missing_organizer_email_count=len(missing_organizer_session_ids),
    )
    return specs, preview


def preview_mailing_recipients(*, visit_date=None, family=None):
    """Return recipient counts for confirmed registrations matching the filters."""
    _specs, preview = _recipient_specs(visit_date=visit_date, family=family)
    return preview


def _validated_content(subject, body_html):
    subject = " ".join(str(subject or "").split())
    if not subject:
        raise ValueError("L’objet du publipostage est obligatoire.")
    if len(subject) > 255:
        raise ValueError("L’objet du publipostage ne doit pas dépasser 255 caractères.")
    cleaned_html = sanitize_rich_html(body_html)
    body_text = rich_html_to_text(cleaned_html)
    if not body_text:
        raise ValueError("Le contenu du publipostage est obligatoire.")
    return subject, cleaned_html, body_text


def _same_idempotent_request(
    campaign, *, subject, body_html, visit_date, family_filter
):
    return (
        campaign.subject == subject
        and campaign.body_html == body_html
        and campaign.visit_date == visit_date
        and campaign.family_filter == family_filter
    )


def create_mailing_campaign(
    *,
    subject,
    body_html,
    created_by=None,
    visit_date=None,
    family=None,
    idempotency_key=None,
):
    """Freeze recipients and personalized context without sending messages."""
    subject, cleaned_html, body_text = _validated_content(subject, body_html)
    visit_date = _visit_date(visit_date)
    family_filter, family_label = _family_values(family)
    idempotency_key = str(idempotency_key or "").strip() or None
    if idempotency_key and len(idempotency_key) > 100:
        raise ValueError("La clé d’idempotence ne doit pas dépasser 100 caractères.")

    if idempotency_key:
        existing = MailingCampaign.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing:
            if not _same_idempotent_request(
                existing,
                subject=subject,
                body_html=cleaned_html,
                visit_date=visit_date,
                family_filter=family_filter,
            ):
                raise ValueError("Cette clé d’idempotence est déjà utilisée pour un autre envoi.")
            return existing

    specs, preview = _recipient_specs(visit_date=visit_date, family=family)
    if preview.total_count == 0:
        raise ValueError("Aucun destinataire ne correspond aux filtres choisis.")

    try:
        with transaction.atomic():
            campaign = MailingCampaign.objects.create(
                idempotency_key=idempotency_key,
                subject=subject,
                body_html=cleaned_html,
                body_text=body_text,
                visit_date=visit_date,
                family_filter=family_filter,
                family_label=family_label,
                created_by=created_by,
            )
            MailingDelivery.objects.bulk_create(
                [
                    MailingDelivery(
                        campaign=campaign,
                        recipient_kind=spec.recipient_kind,
                        recipient=spec.recipient,
                        recipient_name=spec.recipient_name,
                        registration_id=spec.registration_id,
                        dedupe_key=spec.dedupe_key,
                        context_snapshot=spec.context_snapshot,
                    )
                    for spec in specs
                ]
            )
    except IntegrityError:
        if not idempotency_key:
            raise
        campaign = MailingCampaign.objects.get(idempotency_key=idempotency_key)
        if not _same_idempotent_request(
            campaign,
            subject=subject,
            body_html=cleaned_html,
            visit_date=visit_date,
            family_filter=family_filter,
        ):
            raise ValueError(
                "Cette clé d’idempotence est déjà utilisée pour un autre envoi."
            ) from None
    return campaign


def _render_delivery(delivery):
    context = {
        "campaign": delivery.campaign,
        "delivery": delivery,
        "snapshot": delivery.context_snapshot,
    }
    template = (
        "mailing_teacher"
        if delivery.recipient_kind == MailingDelivery.RecipientKind.TEACHER
        else "mailing_organizer"
    )
    return (
        render_to_string(f"emails/{template}.txt", context),
        render_to_string(f"emails/{template}.html", context),
    )


def send_mailing_delivery(delivery_or_id, *, retry_failed=False):
    """Send one frozen delivery once; return ``(delivery, attempted)``."""
    delivery_id = (
        delivery_or_id.pk
        if isinstance(delivery_or_id, MailingDelivery)
        else delivery_or_id
    )
    with transaction.atomic():
        delivery = (
            MailingDelivery.objects.select_for_update()
            .select_related("campaign")
            .get(pk=delivery_id)
        )
        if delivery.status in {
            MailingDelivery.Status.SENT,
            MailingDelivery.Status.SENDING,
        }:
            return delivery, False
        if delivery.status == MailingDelivery.Status.FAILED and not retry_failed:
            return delivery, False
        delivery.status = MailingDelivery.Status.SENDING
        delivery.attempts += 1
        delivery.last_attempted_at = timezone.now()
        delivery.error_summary = ""
        delivery.save(
            update_fields=(
                "status",
                "attempts",
                "last_attempted_at",
                "error_summary",
            )
        )

    try:
        text_body, html_body = _render_delivery(delivery)
        message = EmailMultiAlternatives(
            subject=delivery.campaign.subject,
            body=text_body,
            to=[delivery.recipient],
        )
        message.attach_alternative(html_body, "text/html")
        sent_count = message.send(fail_silently=False)
        if sent_count != 1:
            raise RuntimeError("Le serveur de courriel n’a accepté aucun message.")
    except Exception as error:  # SMTP backends expose several exception types.
        status = MailingDelivery.Status.FAILED
        error_summary = _safe_error_summary(error, delivery.recipient)
        sent_at = None
    else:
        status = MailingDelivery.Status.SENT
        error_summary = ""
        sent_at = timezone.now()

    with transaction.atomic():
        current = MailingDelivery.objects.select_for_update().get(pk=delivery_id)
        if current.status == MailingDelivery.Status.SENT:
            return current, True
        current.status = status
        current.error_summary = error_summary
        current.sent_at = sent_at
        current.save(update_fields=("status", "error_summary", "sent_at"))
    return current, True


def _complete_campaign(campaign_id):
    with transaction.atomic():
        campaign = MailingCampaign.objects.select_for_update().get(pk=campaign_id)
        total = campaign.deliveries.count()
        sent = campaign.deliveries.filter(status=MailingDelivery.Status.SENT).count()
        failed = campaign.deliveries.filter(status=MailingDelivery.Status.FAILED).count()
        unfinished = campaign.deliveries.filter(
            status__in=(
                MailingDelivery.Status.PENDING,
                MailingDelivery.Status.SENDING,
            )
        ).exists()
        if total and sent == total:
            campaign.status = MailingCampaign.Status.SENT
        elif total and failed == total:
            campaign.status = MailingCampaign.Status.FAILED
        elif unfinished:
            campaign.status = MailingCampaign.Status.SENDING
        else:
            campaign.status = MailingCampaign.Status.PARTIAL
        campaign.completed_at = None if unfinished else timezone.now()
        campaign.save(update_fields=("status", "completed_at"))
    return campaign, sent, failed


def send_mailing_campaign(campaign_or_id, *, retry_failed=False):
    """Synchronously send each pending delivery without ever resending a success."""
    campaign_id = (
        campaign_or_id.pk
        if isinstance(campaign_or_id, MailingCampaign)
        else campaign_or_id
    )
    with transaction.atomic():
        campaign = MailingCampaign.objects.select_for_update().get(pk=campaign_id)
        if campaign.started_at is None:
            campaign.started_at = timezone.now()
        campaign.status = MailingCampaign.Status.SENDING
        campaign.completed_at = None
        campaign.save(update_fields=("started_at", "status", "completed_at"))

    stale_before = timezone.now() - timedelta(
        seconds=max(settings.EMAIL_TIMEOUT * 2, 120)
    )
    MailingDelivery.objects.filter(
        campaign_id=campaign_id,
        status=MailingDelivery.Status.SENDING,
    ).filter(
        Q(last_attempted_at__lt=stale_before) | Q(last_attempted_at__isnull=True)
    ).update(
        status=MailingDelivery.Status.FAILED,
        error_summary=(
            "Envoi interrompu avant confirmation. Une relance explicite est nécessaire."
        ),
        sent_at=None,
    )

    statuses = [MailingDelivery.Status.PENDING]
    if retry_failed:
        statuses.append(MailingDelivery.Status.FAILED)
    delivery_ids = list(
        MailingDelivery.objects.filter(
            campaign_id=campaign_id, status__in=statuses
        ).values_list("pk", flat=True)
    )
    attempted = 0
    for delivery_id in delivery_ids:
        _delivery, was_attempted = send_mailing_delivery(
            delivery_id, retry_failed=retry_failed
        )
        attempted += int(was_attempted)

    campaign, sent, failed = _complete_campaign(campaign_id)
    return MailingSendResult(
        campaign=campaign,
        sent_count=sent,
        failed_count=failed,
        skipped_count=campaign.deliveries.count() - attempted,
    )


def create_and_send_mailing(
    *,
    subject,
    body_html,
    created_by=None,
    visit_date=None,
    family=None,
    idempotency_key=None,
):
    """Freeze and synchronously send one campaign, idempotently when a key is supplied."""
    campaign = create_mailing_campaign(
        subject=subject,
        body_html=body_html,
        created_by=created_by,
        visit_date=visit_date,
        family=family,
        idempotency_key=idempotency_key,
    )
    return send_mailing_campaign(campaign)
