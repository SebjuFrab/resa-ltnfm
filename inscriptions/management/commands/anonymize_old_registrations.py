from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from communication.models import EmailLog, MailingDelivery
from inscriptions.models import (
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
    Teacher,
)


class Command(BaseCommand):
    help = "Anonymise les inscriptions dépassant la durée de conservation configurée."

    def add_arguments(self, parser):
        parser.add_argument(
            "--before",
            help=(
                "Anonymise les visites antérieures à cette date (AAAA-MM-JJ), "
                "sinon applique DATA_RETENTION_DAYS."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compte les inscriptions sans les modifier.",
        )

    def _cutoff(self, value):
        if not value:
            return timezone.localdate() - timedelta(days=settings.DATA_RETENTION_DAYS)
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as error:
            raise CommandError("--before doit respecter le format AAAA-MM-JJ.") from error

    def handle(self, *args, **options):
        cutoff = self._cutoff(options["before"])
        candidates = Registration.objects.filter(
            visit_date__lt=cutoff,
            anonymized_at__isnull=True,
        )
        count = candidates.count()
        if options["dry_run"]:
            self.stdout.write(f"{count} inscription(s) seraient anonymisée(s).")
            return

        now = timezone.now()
        old_teacher_ids = set()
        institution_ids = set()
        with transaction.atomic():
            registrations = list(
                candidates.select_for_update()
                .select_related("institution", "teacher")
                .order_by("pk")
            )
            redacted_registration_ids = {registration.pk for registration in registrations}
            redacted_group_codes = {
                registration.group_code.casefold()
                for registration in registrations
                if registration.group_code
            }
            for registration in registrations:
                old_teacher_ids.add(registration.teacher_id)
                institution_ids.add(registration.institution_id)
                anonymous_teacher, _created = Teacher.objects.get_or_create(
                    institution=registration.institution,
                    email=f"anonymized+{registration.institution_id}@example.invalid",
                    defaults={
                        "first_name": "Contact",
                        "last_name": "anonymisé",
                        "phone": "",
                    },
                )
                update_values = {
                    "teacher": anonymous_teacher,
                    "group_name": "Groupe anonymisé",
                    "special_needs": "",
                    "level_comment": "",
                    "comment": "",
                    "token_revoked_at": registration.token_revoked_at or now,
                    "anonymized_at": now,
                    "updated_at": now,
                }
                if registration.status == Registration.Status.DRAFT:
                    update_values.update(
                        status=Registration.Status.CANCELLED,
                        draft_expires_at=None,
                        cancelled_at=registration.cancelled_at or now,
                    )
                    Reservation.objects.filter(
                        registration=registration,
                        status=Reservation.Status.ACTIVE,
                    ).update(
                        status=Reservation.Status.CANCELLED,
                        cancelled_at=now,
                        updated_at=now,
                    )
                Registration.objects.filter(pk=registration.pk).update(**update_values)
                EmailLog.objects.filter(registration=registration).update(
                    recipient="anonymized@example.invalid",
                    error_summary="",
                )
                MailingDelivery.objects.filter(registration=registration).update(
                    recipient="anonymized@example.invalid",
                    recipient_name="",
                    context_snapshot={"redacted": True},
                    error_summary="",
                )
                RegistrationEvent.objects.filter(registration=registration).update(
                    changes={"redacted": True}
                )
                RegistrationEvent.objects.create(
                    registration_id=registration.pk,
                    event_type=RegistrationEvent.Type.ANONYMIZED,
                    actor_kind=RegistrationEvent.ActorKind.SYSTEM,
                    changes={"retention_days": settings.DATA_RETENTION_DAYS},
                )

            organizer_deliveries = MailingDelivery.objects.select_for_update().filter(
                recipient_kind=MailingDelivery.RecipientKind.ORGANIZER
            )
            for delivery in organizer_deliveries:
                snapshot = delivery.context_snapshot or {}
                changed = False
                kept_sessions = []
                for session in snapshot.get("sessions", []):
                    kept_groups = []
                    for group in session.get("groups", []):
                        registration_id = group.get("registration_id")
                        group_code = str(group.get("group_code") or "").casefold()
                        should_redact = (
                            registration_id in redacted_registration_ids
                            or group_code in redacted_group_codes
                        )
                        if should_redact:
                            changed = True
                        else:
                            kept_groups.append(group)
                    if kept_groups:
                        session = {
                            **session,
                            "groups": kept_groups,
                            "total_count": sum(
                                int(group.get("total_count") or 0)
                                for group in kept_groups
                            ),
                        }
                        kept_sessions.append(session)
                    elif session.get("groups"):
                        changed = True
                if changed:
                    delivery.context_snapshot = {"sessions": kept_sessions}
                    delivery.error_summary = ""
                    update_fields = ["context_snapshot", "error_summary"]
                    if not kept_sessions:
                        delivery.recipient = "anonymized@example.invalid"
                        delivery.recipient_name = ""
                        update_fields.extend(("recipient", "recipient_name"))
                    delivery.save(update_fields=update_fields)

            Teacher.objects.filter(
                pk__in=old_teacher_ids,
                registrations__isnull=True,
            ).delete()
            for institution_id in institution_ids:
                has_current_registration = Registration.objects.filter(
                    institution_id=institution_id,
                    anonymized_at__isnull=True,
                ).exists()
                if not has_current_registration:
                    Institution.objects.filter(pk=institution_id).update(
                        phone="", administrative_email=""
                    )

        self.stdout.write(self.style.SUCCESS(f"{count} inscription(s) anonymisée(s)."))
