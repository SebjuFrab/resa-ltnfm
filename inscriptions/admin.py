from datetime import date

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.core.exceptions import ValidationError
from django.db import transaction
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import ngettext
from django.views.decorators.debug import sensitive_variables

from catalogue.models import SchoolLevel
from communication.models import EmailLog
from communication.services import schedule_registration_email
from inscriptions.services.registration import (
    RegistrationError,
    cancel_registration,
    update_registration,
)
from inscriptions.services.tokens import rotate_registration_token

from .models import (
    GroupFamily,
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
    Teacher,
)


class RegistrationBulkUpdateForm(forms.Form):
    apply_family = forms.BooleanField(label="Modifier la famille", required=False)
    family = forms.ModelChoiceField(
        label="Nouvelle famille",
        queryset=GroupFamily.objects.order_by("sort_order", "name"),
        required=False,
        empty_label="Aucune famille (effacer)",
    )
    apply_school_level = forms.BooleanField(label="Modifier le niveau", required=False)
    school_level = forms.ModelChoiceField(
        label="Nouveau niveau",
        queryset=SchoolLevel.objects.order_by("sort_order", "label"),
        required=False,
        empty_label="Sélectionner",
    )
    apply_visit_date = forms.BooleanField(label="Modifier le jour de visite", required=False)
    visit_date = forms.DateField(
        label="Nouveau jour de visite",
        required=False,
        input_formats=("%Y-%m-%d",),
        widget=forms.Select(
            choices=(
                ("", "Sélectionner"),
                *(
                    (value, date.fromisoformat(value).strftime("%d/%m/%Y"))
                    for value in settings.EVENT_DATES
                ),
            )
        ),
    )
    apply_student_count = forms.BooleanField(
        label="Modifier le nombre d'élèves",
        required=False,
    )
    student_count = forms.IntegerField(
        label="Nouveau nombre d'élèves",
        min_value=1,
        max_value=500,
        required=False,
    )
    apply_chaperone_count = forms.BooleanField(
        label="Modifier le nombre d'accompagnateurs",
        required=False,
    )
    chaperone_count = forms.IntegerField(
        label="Nouveau nombre d'accompagnateurs",
        min_value=0,
        max_value=100,
        required=False,
    )
    apply_special_needs = forms.BooleanField(
        label="Modifier les besoins particuliers",
        required=False,
    )
    special_needs = forms.CharField(
        label="Nouveaux besoins particuliers",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    apply_level_comment = forms.BooleanField(
        label="Modifier la remarque sur le niveau",
        required=False,
    )
    level_comment = forms.CharField(
        label="Nouvelle remarque sur le niveau",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    apply_comment = forms.BooleanField(label="Modifier le commentaire", required=False)
    comment = forms.CharField(
        label="Nouveau commentaire",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    field_pairs = (
        ("apply_family", "family"),
        ("apply_school_level", "school_level"),
        ("apply_visit_date", "visit_date"),
        ("apply_student_count", "student_count"),
        ("apply_chaperone_count", "chaperone_count"),
        ("apply_special_needs", "special_needs"),
        ("apply_level_comment", "level_comment"),
        ("apply_comment", "comment"),
    )

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(flag_name) for flag_name, _value_name in self.field_pairs):
            raise forms.ValidationError(
                "Cochez au moins un champ à modifier avant de continuer."
            )
        required_values = {
            "apply_school_level": ("school_level", "Sélectionnez le nouveau niveau."),
            "apply_visit_date": ("visit_date", "Sélectionnez le nouveau jour."),
            "apply_student_count": (
                "student_count",
                "Indiquez le nouveau nombre d'élèves.",
            ),
            "apply_chaperone_count": (
                "chaperone_count",
                "Indiquez le nouveau nombre d'accompagnateurs.",
            ),
        }
        for flag_name, (value_name, message) in required_values.items():
            if cleaned.get(flag_name) and cleaned.get(value_name) is None:
                self.add_error(value_name, message)
        return cleaned


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
    actions = ("bulk_update_registrations", "cancel_selected", "resend_confirmation")
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

    @admin.action(
        description="Modifier les groupes sélectionnés",
        permissions=("change",),
    )
    def bulk_update_registrations(self, request, queryset):
        form = (
            RegistrationBulkUpdateForm(request.POST)
            if "apply_bulk_update" in request.POST
            else RegistrationBulkUpdateForm()
        )
        if "apply_bulk_update" in request.POST and form.is_valid():
            cancelled_count = queryset.filter(status=Registration.Status.CANCELLED).count()
            if cancelled_count:
                form.add_error(
                    None,
                    ngettext(
                        "%d groupe sélectionné est annulé et ne peut plus être modifié.",
                        "%d groupes sélectionnés sont annulés et ne peuvent plus être modifiés.",
                        cancelled_count,
                    )
                    % cancelled_count,
                )

            if form.cleaned_data.get("apply_visit_date"):
                incompatible_reservations = Reservation.objects.filter(
                    registration__in=queryset,
                    status=Reservation.Status.ACTIVE,
                ).exclude(session__date=form.cleaned_data["visit_date"])
                if incompatible_reservations.exists():
                    form.add_error(
                        "visit_date",
                        (
                            "Au moins un groupe possède une animation réservée un autre "
                            "jour. Modifiez d'abord son programme."
                        ),
                    )

        if "apply_bulk_update" in request.POST and form.is_valid():
            update_values = {
                value_name: form.cleaned_data[value_name]
                for flag_name, value_name in form.field_pairs
                if form.cleaned_data.get(flag_name)
            }
            applied_labels = [
                form.fields[value_name].label.lower()
                for flag_name, value_name in form.field_pairs
                if form.cleaned_data.get(flag_name)
            ]
            changed = 0
            try:
                with transaction.atomic():
                    for registration in queryset.select_related(
                        "institution", "teacher", "school_level", "family"
                    ):
                        updated = update_registration(
                            registration,
                            **update_values,
                            actor_kind=RegistrationEvent.ActorKind.STAFF,
                            actor_user=request.user,
                        )
                        self.log_change(
                            request,
                            updated,
                            "Modification groupée : " + ", ".join(applied_labels) + ".",
                        )
                        changed += 1
            except (RegistrationError, ValidationError) as error:
                if isinstance(error, ValidationError):
                    error_message = " ".join(error.messages)
                else:
                    error_message = str(error)
                form.add_error(None, error_message)
            else:
                self.message_user(
                    request,
                    ngettext(
                        "%d groupe a été modifié sans envoi de courriel.",
                        "%d groupes ont été modifiés sans envoi de courriel.",
                        changed,
                    )
                    % changed,
                    messages.SUCCESS,
                )
                return None

        context = {
            **self.admin_site.each_context(request),
            "title": "Modifier plusieurs groupes",
            "opts": self.model._meta,
            "queryset": queryset,
            "selected_count": queryset.count(),
            "form": form,
            "field_rows": [
                {"apply": form[flag_name], "value": form[value_name]}
                for flag_name, value_name in form.field_pairs
            ],
            "action_name": "bulk_update_registrations",
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "select_across": request.POST.get("select_across", "0"),
            "media": self.media + form.media,
            "bulk_notice": (
                "Seuls les champs cochés seront remplacés sur tous les groupes "
                "sélectionnés. Cette action n'envoie aucun courriel."
            ),
        }
        return TemplateResponse(request, "admin/bulk_update_selected.html", context)

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
