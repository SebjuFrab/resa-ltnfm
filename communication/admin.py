from django.contrib import admin

from inscriptions.models import Registration

from .mailing import send_mailing_campaign
from .models import EmailLog, MailingCampaign, MailingDelivery
from .services import send_registration_email


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    actions = ("retry_delivery",)
    list_display = ("created_at", "kind", "recipient", "status", "sent_at")
    list_filter = ("kind", "status", "created_at")
    search_fields = (
        "recipient",
        "registration__reference",
        "registration__institution__name",
    )
    readonly_fields = (
        "registration",
        "kind",
        "recipient",
        "status",
        "provider_message_id",
        "error_summary",
        "created_at",
        "sent_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.has_perm("communication.change_emaillog")

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Retenter les échecs sélectionnés")
    def retry_delivery(self, request, queryset):
        attempted = 0
        seen_registrations = set()
        failures = queryset.filter(status=EmailLog.Status.FAILED).order_by(
            "registration_id", "-created_at"
        )
        for previous in failures.select_related("registration"):
            registration = previous.registration
            if (
                registration is None
                or registration.pk in seen_registrations
                or (
                    registration.status == Registration.Status.CANCELLED
                    and previous.kind != EmailLog.Kind.CANCELLATION
                )
            ):
                continue
            seen_registrations.add(registration.pk)
            send_registration_email(registration, previous.kind)
            attempted += 1
        self.message_user(request, f"{attempted} envoi(s) retenté(s).")


class MailingDeliveryInline(admin.TabularInline):
    model = MailingDelivery
    extra = 0
    can_delete = False
    fields = (
        "recipient_kind",
        "recipient",
        "registration",
        "status",
        "attempts",
        "last_attempted_at",
        "sent_at",
        "error_summary",
    )
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(MailingCampaign)
class MailingCampaignAdmin(admin.ModelAdmin):
    actions = ("retry_failed",)
    list_display = (
        "created_at",
        "campaign_subject",
        "audience",
        "visit_date",
        "family_label",
        "status",
        "delivery_count",
        "sent_delivery_count",
        "failed_delivery_count",
        "created_by",
    )
    list_filter = ("audience", "status", "visit_date", "created_at")
    search_fields = (
        "reference",
        "subject",
        "organizer_subject",
        "family_label",
        "deliveries__recipient",
    )
    readonly_fields = (
        "reference",
        "idempotency_key",
        "subject",
        "body_html",
        "body_text",
        "organizer_subject",
        "organizer_body_html",
        "organizer_body_text",
        "audience",
        "visit_date",
        "family_filter",
        "family_label",
        "status",
        "created_by",
        "created_at",
        "started_at",
        "completed_at",
    )
    inlines = (MailingDeliveryInline,)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_send_mailing_permission(self, request):
        return request.user.has_perm("communication.send_mailing")

    @admin.display(description="Objet", ordering="subject")
    def campaign_subject(self, campaign):
        return campaign.subject or campaign.organizer_subject

    @admin.display(description="Destinataires")
    def delivery_count(self, campaign):
        return campaign.deliveries.count()

    @admin.display(description="Envoyés")
    def sent_delivery_count(self, campaign):
        return campaign.sent_count

    @admin.display(description="Échecs")
    def failed_delivery_count(self, campaign):
        return campaign.failed_count

    @admin.action(description="Retenter les échecs", permissions=("send_mailing",))
    def retry_failed(self, request, queryset):
        sent = failed = 0
        for campaign in queryset:
            result = send_mailing_campaign(campaign, retry_failed=True)
            sent += result.sent_count
            failed += result.failed_count
        self.message_user(request, f"État après relance : {sent} envoyé(s), {failed} échec(s).")


@admin.register(MailingDelivery)
class MailingDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "campaign",
        "recipient_kind",
        "recipient",
        "status",
        "attempts",
        "sent_at",
    )
    list_filter = ("recipient_kind", "status", "created_at")
    search_fields = (
        "recipient",
        "campaign__reference",
        "campaign__subject",
        "registration__group_name",
    )
    list_select_related = ("campaign", "registration")
    readonly_fields = (
        "campaign",
        "recipient_kind",
        "recipient",
        "recipient_name",
        "registration",
        "dedupe_key",
        "context_snapshot",
        "status",
        "attempts",
        "error_summary",
        "created_at",
        "last_attempted_at",
        "sent_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
