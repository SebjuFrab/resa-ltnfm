from django.conf import settings
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from communication.models import EmailLog
from communication.services import schedule_registration_email
from inscriptions.services.registration import cancel_registration
from inscriptions.services.tokens import rotate_registration_token

from .models import (
    GroupFamily,
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
    Teacher,
)


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ("name", "institution_type", "postal_code", "city", "department")
    list_filter = ("institution_type", "department")
    search_fields = ("name", "postal_code", "city", "administrative_email")


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ("last_name", "first_name", "institution", "email", "phone")
    search_fields = ("last_name", "first_name", "email", "institution__name")
    autocomplete_fields = ("institution",)


@admin.register(GroupFamily)
class GroupFamilyAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    list_filter = ("is_active",)
    ordering = ("sort_order", "name")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug")


class ReservationInline(admin.TabularInline):
    model = Reservation
    extra = 0
    can_delete = False
    fields = ("session", "student_count", "chaperone_count", "status", "cancelled_at")
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Registration)
class RegistrationAdmin(admin.ModelAdmin):
    actions = ("cancel_selected", "resend_confirmation")
    list_display = (
        "group_code",
        "group_name",
        "institution",
        "teacher",
        "family",
        "visit_date",
        "student_count",
        "chaperone_count",
        "total_participant_count_display",
        "status",
    )
    list_filter = ("status", "visit_date", "school_level", "family")
    search_fields = (
        "reference__exact",
        "group_code",
        "group_name",
        "institution__name",
        "teacher__first_name",
        "teacher__last_name",
        "teacher__email",
    )
    list_select_related = ("institution", "teacher", "school_level", "family")
    date_hierarchy = "visit_date"
    inlines = (ReservationInline,)
    exclude = ("edit_token_digest",)
    readonly_fields = (
        "reference",
        "group_code",
        "institution",
        "teacher",
        "group_name",
        "family",
        "school_level",
        "student_count",
        "chaperone_count",
        "visit_date",
        "special_needs",
        "level_comment",
        "comment",
        "status",
        "draft_expires_at",
        "token_created_at",
        "token_revoked_at",
        "confirmed_at",
        "cancelled_at",
        "anonymized_at",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Effectif total")
    def total_participant_count_display(self, registration):
        return registration.total_participant_count

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Annuler les inscriptions sélectionnées")
    def cancel_selected(self, request, queryset):
        changed = 0
        for registration in queryset.exclude(status=Registration.Status.CANCELLED):
            cancel_registration(
                registration,
                actor_kind=RegistrationEvent.ActorKind.STAFF,
                actor_user=request.user,
            )
            schedule_registration_email(registration, EmailLog.Kind.CANCELLATION)
            changed += 1
        self.message_user(request, f"{changed} inscription(s) annulée(s).")

    @admin.action(description="Renvoyer le courriel de confirmation")
    @sensitive_variables("token", "landing")
    def resend_confirmation(self, request, queryset):
        sent = 0
        for registration in queryset.filter(status=Registration.Status.CONFIRMED):
            edit_url = ""
            if timezone.now() <= settings.REGISTRATION_EDIT_DEADLINE:
                token = rotate_registration_token(
                    registration,
                    actor_kind=RegistrationEvent.ActorKind.STAFF,
                    actor_user=request.user,
                )
                landing = request.build_absolute_uri(
                    reverse(
                        "registration-edit-link",
                        kwargs={"reference": registration.reference},
                    )
                )
                edit_url = f"{landing}#{token}"
            schedule_registration_email(
                registration,
                EmailLog.Kind.CONFIRMATION,
                edit_url=edit_url,
            )
            sent += 1
        self.message_user(request, f"{sent} courriel(s) programmé(s).")


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = (
        "registration",
        "session",
        "student_count",
        "chaperone_count",
        "total_participant_count_display",
        "status",
    )
    list_filter = ("status", "session__date")
    search_fields = ("registration__reference__exact", "registration__group_name")
    list_select_related = ("registration", "session", "session__animation")
    readonly_fields = (
        "registration",
        "session",
        "student_count",
        "chaperone_count",
        "status",
        "cancelled_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Effectif total")
    def total_participant_count_display(self, reservation):
        return reservation.total_participant_count


@admin.register(RegistrationEvent)
class RegistrationEventAdmin(admin.ModelAdmin):
    list_display = ("registration", "event_type", "actor_kind", "actor_user", "created_at")
    list_filter = ("event_type", "actor_kind", "created_at")
    search_fields = ("registration__reference__exact", "registration__group_name")
    list_select_related = ("registration", "actor_user")
    readonly_fields = (
        "registration",
        "event_type",
        "actor_kind",
        "actor_user",
        "changes",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.site_header = "Administration des inscriptions LTNM"
admin.site.site_title = "LTNM"
admin.site.index_title = "Gestion du salon"
