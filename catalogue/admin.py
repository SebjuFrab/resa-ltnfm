from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.core.exceptions import ValidationError
from django.db import transaction
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import ngettext

from .models import Animation, SchoolLevel, Session, Theme
from .services import validate_max_capacity


class AnimationBulkUpdateForm(forms.Form):
    apply_venue_category = forms.BooleanField(
        label="Modifier la catégorie",
        required=False,
    )
    venue_category = forms.ChoiceField(
        label="Nouvelle catégorie",
        required=False,
        choices=(("", "Sélectionner"), *Animation.VenueCategory.choices),
    )
    apply_themes = forms.BooleanField(label="Modifier les thématiques", required=False)
    themes = forms.ModelMultipleChoiceField(
        label="Nouvelles thématiques",
        queryset=Theme.objects.order_by("sort_order", "name"),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 7}),
    )
    apply_recommended_levels = forms.BooleanField(
        label="Modifier les niveaux conseillés",
        required=False,
    )
    recommended_levels = forms.ModelMultipleChoiceField(
        label="Nouveaux niveaux conseillés",
        queryset=SchoolLevel.objects.order_by("sort_order", "label"),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 7}),
    )
    apply_indicative_duration = forms.BooleanField(
        label="Modifier la durée indicative",
        required=False,
    )
    indicative_duration = forms.IntegerField(
        label="Nouvelle durée indicative (minutes)",
        min_value=1,
        required=False,
    )
    apply_short_description = forms.BooleanField(
        label="Modifier la description courte",
        required=False,
    )
    short_description = forms.CharField(
        label="Nouvelle description courte",
        max_length=300,
        required=False,
    )
    apply_description = forms.BooleanField(
        label="Modifier la description",
        required=False,
    )
    description = forms.CharField(
        label="Nouvelle description",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    apply_instructions = forms.BooleanField(label="Modifier les consignes", required=False)
    instructions = forms.CharField(
        label="Nouvelles consignes",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    apply_accessibility = forms.BooleanField(label="Modifier l'accessibilité", required=False)
    accessibility = forms.CharField(
        label="Nouvelle accessibilité",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    apply_is_active = forms.BooleanField(label="Modifier l'état actif", required=False)
    is_active = forms.BooleanField(label="Animations actives", required=False)

    field_pairs = (
        ("apply_venue_category", "venue_category"),
        ("apply_themes", "themes"),
        ("apply_recommended_levels", "recommended_levels"),
        ("apply_indicative_duration", "indicative_duration"),
        ("apply_short_description", "short_description"),
        ("apply_description", "description"),
        ("apply_instructions", "instructions"),
        ("apply_accessibility", "accessibility"),
        ("apply_is_active", "is_active"),
    )

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(flag_name) for flag_name, _value_name in self.field_pairs):
            raise forms.ValidationError(
                "Cochez au moins un champ à modifier avant de continuer."
            )
        required_values = {
            "apply_venue_category": (
                "venue_category",
                "Sélectionnez la nouvelle catégorie.",
            ),
            "apply_themes": (
                "themes",
                "Sélectionnez au moins une thématique.",
            ),
            "apply_indicative_duration": (
                "indicative_duration",
                "Indiquez la nouvelle durée.",
            ),
            "apply_short_description": (
                "short_description",
                "Indiquez la nouvelle description courte.",
            ),
        }
        for flag_name, (value_name, message) in required_values.items():
            if cleaned.get(flag_name) and not cleaned.get(value_name):
                self.add_error(value_name, message)
        return cleaned


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
    actions = ("bulk_update_animations", "duplicate_animations")
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

    @admin.action(
        description="Modifier les animations sélectionnées",
        permissions=("change",),
    )
    def bulk_update_animations(self, request, queryset):
        form = (
            AnimationBulkUpdateForm(request.POST)
            if "apply_bulk_update" in request.POST
            else AnimationBulkUpdateForm()
        )
        if "apply_bulk_update" in request.POST and form.is_valid():
            scalar_fields = (
                "venue_category",
                "indicative_duration",
                "short_description",
                "description",
                "instructions",
                "accessibility",
                "is_active",
            )
            applied_labels = []
            for flag_name, value_name in form.field_pairs:
                if form.cleaned_data.get(flag_name):
                    applied_labels.append(form.fields[value_name].label.lower())

            changed = 0
            with transaction.atomic():
                for animation in queryset.prefetch_related("themes", "recommended_levels"):
                    update_fields = []
                    for field_name in scalar_fields:
                        if not form.cleaned_data.get(f"apply_{field_name}"):
                            continue
                        setattr(animation, field_name, form.cleaned_data[field_name])
                        update_fields.append(field_name)

                    if update_fields:
                        animation.save(update_fields=(*update_fields, "updated_at"))
                    elif form.cleaned_data.get("apply_themes") or form.cleaned_data.get(
                        "apply_recommended_levels"
                    ):
                        animation.updated_at = timezone.now()
                        animation.save(update_fields=("updated_at",))

                    if form.cleaned_data.get("apply_themes"):
                        animation.themes.set(form.cleaned_data["themes"])
                    if form.cleaned_data.get("apply_recommended_levels"):
                        animation.recommended_levels.set(
                            form.cleaned_data["recommended_levels"]
                        )
                    self.log_change(
                        request,
                        animation,
                        "Modification groupée : " + ", ".join(applied_labels) + ".",
                    )
                    changed += 1

            self.message_user(
                request,
                ngettext(
                    "%d animation a été modifiée.",
                    "%d animations ont été modifiées.",
                    changed,
                )
                % changed,
                messages.SUCCESS,
            )
            return None

        context = {
            **self.admin_site.each_context(request),
            "title": "Modifier plusieurs animations",
            "opts": self.model._meta,
            "queryset": queryset,
            "selected_count": queryset.count(),
            "form": form,
            "field_rows": [
                {"apply": form[flag_name], "value": form[value_name]}
                for flag_name, value_name in form.field_pairs
            ],
            "action_name": "bulk_update_animations",
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "select_across": request.POST.get("select_across", "0"),
            "media": self.media + form.media,
            "bulk_notice": (
                "Seuls les champs cochés seront remplacés sur toutes les animations "
                "sélectionnées."
            ),
        }
        return TemplateResponse(request, "admin/bulk_update_selected.html", context)

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
