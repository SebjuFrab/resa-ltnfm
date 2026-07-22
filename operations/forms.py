from datetime import date
from pathlib import Path

from django import forms
from django.conf import settings
from django.db.models import Q

from catalogue.models import Category, SchoolLevel, Session
from inscriptions.choices import department_form_choices
from inscriptions.codes import generate_unique_group_code, normalize_group_code
from inscriptions.models import GroupFamily, Institution, Registration, Reservation


class SessionImportForm(forms.Form):
    file = forms.FileField(
        label="Fichier CSV des animations",
        help_text=(
            "UTF-8 ou Windows-1252, séparateur point-virgule conseillé, "
            "500 lignes et 2 Mo maximum."
        ),
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if Path(upload.name).suffix.lower() != ".csv":
            raise forms.ValidationError("Sélectionnez un fichier portant l'extension .csv.")
        if upload.size > 2 * 1024 * 1024:
            raise forms.ValidationError("Le fichier ne doit pas dépasser 2 Mo.")
        return upload


class RegistrationSearchForm(forms.Form):
    q = forms.CharField(
        label="Recherche",
        required=False,
        max_length=200,
        widget=forms.TextInput(
            attrs={"placeholder": "Référence, établissement, professeur ou courriel"}
        ),
    )
    date = forms.DateField(
        label="Jour",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    status = forms.ChoiceField(label="Statut", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from inscriptions.models import Registration

        self.fields["status"].choices = [("", "Tous")] + list(Registration.Status.choices)


class ExportForm(forms.Form):
    class ExportType:
        REGISTRATIONS = "registrations"
        RESERVATIONS = "reservations"
        SESSIONS = "sessions"

    export_type = forms.ChoiceField(
        label="Données",
        choices=(
            (ExportType.REGISTRATIONS, "Inscriptions"),
            (ExportType.RESERVATIONS, "Réservations"),
            (ExportType.SESSIONS, "Synthèse par séance"),
        ),
    )
    date = forms.DateField(
        label="Jour (facultatif)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    delimiter = forms.ChoiceField(
        label="Séparateur",
        choices=(("semicolon", "Point-virgule"), ("comma", "Virgule")),
    )


class AnimationFilterForm(forms.Form):
    q = forms.CharField(
        label="Recherche",
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "Titre, lieu ou responsable"}),
    )
    date = forms.ChoiceField(label="Jour", required=False)
    category = forms.ModelChoiceField(
        label="Catégorie", queryset=Category.objects.none(), required=False
    )
    level = forms.ModelChoiceField(
        label="Niveau", queryset=SchoolLevel.objects.none(), required=False
    )
    starts_after = forms.TimeField(
        label="À partir de",
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}),
    )
    ends_before = forms.TimeField(
        label="Avant",
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}),
    )
    status = forms.ChoiceField(label="Statut", required=False)
    available_only = forms.BooleanField(
        label="Places disponibles uniquement", required=False
    )

    def __init__(self, *args, fixed_date=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixed_date = fixed_date
        self.fields["date"].choices = [("", "Tous les jours")] + [
            (value, date.fromisoformat(value).strftime("%d/%m/%Y"))
            for value in settings.EVENT_DATES
        ]
        if fixed_date:
            self.fields.pop("date")
        self.fields["category"].queryset = Category.objects.filter(is_active=True)
        self.fields["level"].queryset = SchoolLevel.objects.filter(is_active=True)
        self.fields["status"].choices = [("", "Tous")] + list(Session.Status.choices)

    def apply(self, queryset):
        if not self.is_valid():
            return queryset
        if self.fixed_date:
            queryset = queryset.filter(date=self.fixed_date)
        elif selected_date := self.cleaned_data.get("date"):
            queryset = queryset.filter(date=date.fromisoformat(selected_date))
        if query := self.cleaned_data.get("q", "").strip():
            queryset = queryset.filter(
                Q(animation__title__icontains=query)
                | Q(animation__short_description__icontains=query)
                | Q(location__icontains=query)
                | Q(organizer__icontains=query)
                | Q(organizer_email__icontains=query)
            )
        if category := self.cleaned_data.get("category"):
            queryset = queryset.filter(animation__category=category)
        if level := self.cleaned_data.get("level"):
            queryset = queryset.filter(animation__recommended_levels=level)
        if starts_after := self.cleaned_data.get("starts_after"):
            queryset = queryset.filter(starts_at__gte=starts_after)
        if ends_before := self.cleaned_data.get("ends_before"):
            queryset = queryset.filter(ends_at__lte=ends_before)
        if status := self.cleaned_data.get("status"):
            queryset = queryset.filter(status=status)
        if self.cleaned_data.get("available_only"):
            queryset = queryset.filter(_remaining_capacity__gt=0)
        return queryset.distinct()


class StaffRegistrationForm(forms.Form):
    existing_institution = forms.ModelChoiceField(
        label="Établissement existant",
        queryset=Institution.objects.none(),
        required=False,
        help_text="Sélectionnez un établissement ou renseignez les champs du nouvel établissement.",
    )
    institution_name = forms.CharField(
        label="Nom de l'établissement", max_length=200, required=False
    )
    institution_city = forms.CharField(label="Commune", max_length=120, required=False)
    institution_department = forms.ChoiceField(
        label="Département",
        choices=department_form_choices(),
        required=False,
    )
    institution_type = forms.ChoiceField(
        label="Type du nouvel établissement",
        required=False,
        choices=(
            (Institution.Type.AGRICULTURAL, "Lycée / établissement agricole"),
            (Institution.Type.HIGH_SCHOOL, "Lycée général ou professionnel"),
            (Institution.Type.HIGHER_EDUCATION, "Enseignement supérieur"),
            (Institution.Type.OTHER, "Autre"),
        ),
    )
    teacher_last_name = forms.CharField(label="Nom de l'enseignant", max_length=100)
    teacher_first_name = forms.CharField(label="Prénom", max_length=100)
    teacher_email = forms.EmailField(label="Courriel")
    teacher_phone = forms.CharField(label="Téléphone", max_length=30)
    group_code = forms.CharField(
        label="Nom code",
        max_length=80,
        required=False,
        help_text="Code unique et facile à dicter, généré à partir d'un aliment.",
    )
    family = forms.ModelChoiceField(
        label="Famille", queryset=GroupFamily.objects.none()
    )
    school_level = forms.ModelChoiceField(
        label="Niveau", queryset=SchoolLevel.objects.none()
    )
    visit_date = forms.ChoiceField(label="Jour de visite")
    student_count = forms.IntegerField(
        label="Nombre d'étudiants", min_value=1, max_value=500
    )
    chaperone_count = forms.IntegerField(
        label="Nombre de professeurs / accompagnateurs",
        min_value=0,
        max_value=100,
        initial=0,
    )
    level_comment = forms.CharField(
        label="Remarque sur le niveau",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    comment = forms.CharField(
        label="Remarque générale",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, registration=None, **kwargs):
        self.registration = registration
        super().__init__(*args, **kwargs)
        self.fields["existing_institution"].queryset = Institution.objects.order_by(
            "name", "city"
        )
        family_queryset = GroupFamily.objects.filter(is_active=True)
        if registration and registration.family_id:
            family_queryset = GroupFamily.objects.filter(
                Q(is_active=True) | Q(pk=registration.family_id)
            )
        self.fields["family"].queryset = family_queryset.order_by("sort_order", "name")
        level_queryset = SchoolLevel.objects.filter(is_active=True)
        if registration and registration.school_level_id:
            level_queryset = SchoolLevel.objects.filter(
                Q(is_active=True) | Q(pk=registration.school_level_id)
            )
        self.fields["school_level"].queryset = level_queryset.order_by(
            "sort_order", "label"
        )
        self.fields["visit_date"].choices = [
            (value, date.fromisoformat(value).strftime("%d/%m/%Y"))
            for value in settings.EVENT_DATES
        ]
        if registration and not self.is_bound:
            self.initial.update(
                {
                    "existing_institution": registration.institution,
                    "institution_type": registration.institution.institution_type,
                    "teacher_last_name": registration.teacher.last_name,
                    "teacher_first_name": registration.teacher.first_name,
                    "teacher_email": registration.teacher.email,
                    "teacher_phone": registration.teacher.phone,
                    "group_code": registration.group_code,
                    "family": registration.family,
                    "school_level": registration.school_level,
                    "visit_date": registration.visit_date.isoformat(),
                    "student_count": registration.student_count,
                    "chaperone_count": registration.chaperone_count,
                    "level_comment": registration.level_comment,
                    "comment": registration.comment,
                }
            )
        elif not self.is_bound:
            self.initial["group_code"] = generate_unique_group_code()
            self.initial["institution_type"] = Institution.Type.AGRICULTURAL

    def clean_group_code(self):
        code = normalize_group_code(self.cleaned_data.get("group_code", ""))
        if not code:
            code = generate_unique_group_code(
                excluding_registration_id=getattr(self.registration, "pk", None)
            )
        used = Registration.objects.filter(group_code__iexact=code)
        if self.registration:
            used = used.exclude(pk=self.registration.pk)
        if used.exists():
            raise forms.ValidationError("Ce code est déjà utilisé par un autre groupe.")
        return code

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("existing_institution"):
            for field_name, message in (
                ("institution_name", "Indiquez le nom de l'établissement."),
                ("institution_city", "Indiquez la commune."),
                ("institution_department", "Indiquez le département."),
                ("institution_type", "Sélectionnez le type d'établissement."),
            ):
                if not cleaned.get(field_name):
                    self.add_error(field_name, message)
        selected_date = cleaned.get("visit_date")
        if selected_date:
            cleaned["visit_date"] = date.fromisoformat(selected_date)
        return cleaned


class StaffPlanningForm(forms.Form):
    def __init__(self, *args, registration, sessions, **kwargs):
        super().__init__(*args, **kwargs)
        self.registration = registration
        self.sessions = list(sessions)
        current = {
            reservation.session_id: reservation
            for reservation in registration.reservations.filter(
                status=Reservation.Status.ACTIVE
            )
        }
        group_total = registration.total_participant_count
        for session in self.sessions:
            reservation = current.get(session.pk)
            currently_selected = reservation is not None
            recoverable = reservation.total_participant_count if reservation else 0
            available_for_group = session.remaining_capacity + recoverable
            selectable = (
                currently_selected
                or (
                    session.status == Session.Status.OPEN
                    and session.animation.is_active
                    and available_for_group >= group_total
                )
            )
            self.fields[self.field_name(session.pk)] = forms.BooleanField(
                label=f"Réserver le groupe complet ({group_total} personnes)",
                required=False,
                initial=currently_selected,
                disabled=not selectable,
                widget=forms.CheckboxInput(
                    attrs={"aria-describedby": f"capacity-{session.pk}"}
                ),
            )

    @staticmethod
    def field_name(session_id):
        return f"session_{session_id}"

    def selected_session_ids(self):
        if not self.is_valid():
            raise ValueError("Le formulaire doit être valide.")
        return {
            session.pk
            for session in self.sessions
            if self.cleaned_data.get(self.field_name(session.pk), False)
        }


class MailingForm(forms.Form):
    visit_date = forms.ChoiceField(label="Jour", required=False)
    family = forms.ModelChoiceField(
        label="Famille", queryset=GroupFamily.objects.none(), required=False
    )
    subject = forms.CharField(label="Objet", max_length=200)
    body_html = forms.CharField(
        label="Contenu enrichi", widget=forms.Textarea, max_length=50_000
    )
    confirm_missing = forms.BooleanField(
        label="Je confirme l'envoi malgré les adresses manquantes.",
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["visit_date"].choices = [("", "Tous les jours du salon")] + [
            (value, date.fromisoformat(value).strftime("%d/%m/%Y"))
            for value in settings.EVENT_DATES
        ]
        self.fields["family"].queryset = GroupFamily.objects.filter(
            is_active=True
        ).order_by("sort_order", "name")

    def clean_visit_date(self):
        value = self.cleaned_data["visit_date"]
        return date.fromisoformat(value) if value else None
