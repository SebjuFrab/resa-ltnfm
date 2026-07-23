from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import ngettext

from .models import Animation, SchoolLevel, Session, Theme
from .services import validate_max_capacity


def _available_slug(animation):
    base = slugify(f"{animation.slug or animation.title}-copie") or "animation-copie"
    max_length = Animation._meta.get_field("slug").max_length
    base = base[:max_length]
    candidate = base
    counter = 2
    while Animation.objects.filter(slug=candidate).exists():
        suffix = f"-{counter}"
        candidate = f"{base[: max_length - len(suffix)]}{suffix}"
        counter += 1
    return candidate


@admin.register(SchoolLevel)
class SchoolLevelAdmin(admin.ModelAdmin):
    list_display = ("label", "code", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    list_filter = ("is_active",)
    ordering = ("sort_order", "label")
    search_fields = ("label", "code")


@admin.register(Theme)
class ThemeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    list_filter = ("is_active",)
    ordering = ("sort_order", "name")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

    def has_delete_permission(self, request, obj=None):
        return False


class SessionInline(admin.TabularInline):
    model = Session
    extra = 0
    fields = (
        "date",
        "starts_at",
        "ends_at",
        "location",
        "max_capacity",
        "status",
        "organizer",
        "organizer_email",
    )
    ordering = ("date", "starts_at")
    show_change_link = True
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SessionAdminForm(forms.ModelForm):
    class Meta:
        model = Session
        fields = (
            "animation",
            "date",
            "starts_at",
            "ends_at",
            "location",
            "max_capacity",
            "status",
            "organizer",
            "organizer_email",
            "internal_comment",
        )

    def clean_max_capacity(self):
        capacity = self.cleaned_data["max_capacity"]
        if self.instance.pk:
            validate_max_capacity(self.instance.pk, capacity)
        return capacity


@admin.register(Animation)
class AnimationAdmin(admin.ModelAdmin):
    actions = ("duplicate_animations",)
    date_hierarchy = "created_at"
    exclude = ("category",)
    filter_horizontal = ("themes", "recommended_levels")
    inlines = (SessionInline,)
    list_display = (
        "title",
        "venue_category",
        "indicative_duration",
        "is_active",
        "updated_at",
    )
    list_editable = ("is_active",)
    list_filter = ("is_active", "venue_category", "themes", "recommended_levels")
    prepopulated_fields = {"slug": ("title",)}
    search_fields = ("title", "short_description", "description", "themes__name")

    @admin.action(description="Dupliquer les animations sélectionnées")
    def duplicate_animations(self, request, queryset):
        duplicated = 0
        with transaction.atomic():
            for source in queryset.select_related("category").prefetch_related(
                "themes", "recommended_levels"
            ):
                levels = list(source.recommended_levels.all())
                themes = list(source.themes.all())
                copy = Animation.objects.create(
                    title=f"Copie de {source.title}"[:200],
                    slug=_available_slug(source),
                    short_description=source.short_description,
                    description=source.description,
                    category=source.category,
                    venue_category=source.venue_category,
                    indicative_duration=source.indicative_duration,
                    image=source.image.name if source.image else "",
                    instructions=source.instructions,
                    accessibility=source.accessibility,
                    is_active=False,
                )
                copy.recommended_levels.set(levels)
                copy.themes.set(themes)
                self.log_addition(
                    request,
                    copy,
                    f"Animation dupliquée depuis l’animation {source.pk}.",
                )
                duplicated += 1

        self.message_user(
            request,
            ngettext(
                "%d animation a été dupliquée en brouillon.",
                "%d animations ont été dupliquées en brouillon.",
                duplicated,
            )
            % duplicated,
            messages.SUCCESS,
        )


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    form = SessionAdminForm
    actions = ("open_sessions", "close_sessions", "cancel_sessions")
    autocomplete_fields = ("animation",)
    date_hierarchy = "date"
    list_display = (
        "animation",
        "date",
        "starts_at",
        "ends_at",
        "location",
        "max_capacity",
        "reserved_capacity_display",
        "remaining_capacity_display",
        "status",
    )
    list_editable = ("status",)
    list_filter = ("date", "status", "animation__venue_category", "animation__themes")
    list_select_related = ("animation",)
    ordering = ("date", "starts_at", "animation__title")
    search_fields = (
        "animation__title",
        "location",
        "organizer",
        "organizer_email",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).with_capacities()

    def save_model(self, request, obj, form, change):
        if not change:
            return super().save_model(request, obj, form, change)
        with transaction.atomic():
            locked = Session.objects.select_for_update().get(pk=obj.pk)
            try:
                validate_max_capacity(locked.pk, obj.max_capacity)
            except ValidationError:
                self.message_user(
                    request,
                    "La capacité n’a pas été modifiée car des places sont déjà réservées.",
                    level=messages.ERROR,
                )
                raise
            return super().save_model(request, obj, form, change)

    @admin.display(description="Réservée", ordering="_reserved_capacity")
    def reserved_capacity_display(self, session):
        return session.reserved_capacity

    @admin.display(description="Restante", ordering="_remaining_capacity")
    def remaining_capacity_display(self, session):
        return session.remaining_capacity

    def _set_status(self, request, queryset, status, label_singular, label_plural):
        sessions = list(queryset.exclude(status=status).select_related("animation"))
        updated = queryset.filter(pk__in=[session.pk for session in sessions]).update(
            status=status,
            updated_at=timezone.now(),
        )
        for session in sessions:
            session.status = status
            self.log_change(
                request,
                session,
                f"Statut modifié en « {session.get_status_display()} » par action groupée.",
            )
        self.message_user(
            request,
            ngettext(label_singular, label_plural, updated) % updated,
            messages.SUCCESS,
        )

    @admin.action(description="Ouvrir les séances sélectionnées")
    def open_sessions(self, request, queryset):
        self._set_status(
            request,
            queryset,
            Session.Status.OPEN,
            "%d séance a été ouverte.",
            "%d séances ont été ouvertes.",
        )

    @admin.action(description="Fermer les séances sélectionnées")
    def close_sessions(self, request, queryset):
        self._set_status(
            request,
            queryset,
            Session.Status.CLOSED,
            "%d séance a été fermée.",
            "%d séances ont été fermées.",
        )

    @admin.action(description="Annuler les séances sélectionnées")
    def cancel_sessions(self, request, queryset):
        self._set_status(
            request,
            queryset,
            Session.Status.CANCELLED,
            "%d séance a été annulée.",
            "%d séances ont été annulées.",
        )
