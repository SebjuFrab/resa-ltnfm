from datetime import date

from django import forms
from django.conf import settings

from catalogue.models import Category, SchoolLevel, Session
from inscriptions.choices import department_form_choices
from inscriptions.models import Institution, Registration


class RegistrationIdentityForm(forms.Form):
    existing_institution = forms.ModelChoiceField(
        label="Établissement existant",
        queryset=Institution.objects.none(),
        required=False,
        help_text="Sélectionnez-le, ou renseignez un nouvel établissement ci-dessous.",
    )
    institution_name = forms.CharField(
        label="Nom de l’établissement", max_length=200, required=False
    )
    institution_type = forms.ChoiceField(label="Type d’établissement", required=False)
    institution_address = forms.CharField(label="Adresse", max_length=255, required=False)
    institution_postal_code = forms.CharField(label="Code postal", max_length=10, required=False)
    institution_city = forms.CharField(label="Ville", max_length=120, required=False)
    institution_department = forms.ChoiceField(
        label="Département",
        choices=department_form_choices(),
        required=False,
    )
    institution_phone = forms.CharField(
        label="Téléphone de l’établissement", max_length=30, required=False
    )
    institution_email = forms.EmailField(label="Courriel administratif", required=False)

    teacher_first_name = forms.CharField(label="Prénom du professeur", max_length=100)
    teacher_last_name = forms.CharField(label="Nom du professeur", max_length=100)
    teacher_email = forms.EmailField(label="Courriel du professeur")
    teacher_phone = forms.CharField(label="Téléphone du professeur", max_length=30)

    group_name = forms.CharField(label="Nom ou référence du groupe", max_length=150)
    school_level = forms.ModelChoiceField(
        label="Niveau scolaire", queryset=SchoolLevel.objects.none()
    )
    student_count = forms.IntegerField(label="Nombre d’élèves", min_value=1, max_value=500)
    chaperone_count = forms.IntegerField(
        label="Nombre d’accompagnateurs", min_value=0, max_value=100, initial=0
    )
    visit_date = forms.ChoiceField(label="Jour de visite")
    special_needs = forms.CharField(
        label="Besoins particuliers", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    comment = forms.CharField(
        label="Commentaire", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["existing_institution"].queryset = Institution.objects.order_by(
            "name", "postal_code"
        )
        self.fields["institution_type"].choices = [("", "Sélectionner")] + list(
            Institution._meta.get_field("institution_type").choices
        )
        self.fields["school_level"].queryset = SchoolLevel.objects.filter(is_active=True).order_by(
            "sort_order", "label"
        )
        self.fields["visit_date"].choices = [
            (value, date.fromisoformat(value).strftime("%d/%m/%Y"))
            for value in settings.EVENT_DATES
        ]

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("existing_institution"):
            required = {
                "institution_name": "Indiquez le nom de l’établissement.",
                "institution_type": "Sélectionnez le type d’établissement.",
                "institution_address": "Indiquez l’adresse de l’établissement.",
                "institution_postal_code": "Indiquez le code postal.",
                "institution_city": "Indiquez la ville.",
                "institution_department": "Indiquez le département.",
            }
            for field_name, message in required.items():
                if not cleaned.get(field_name):
                    self.add_error(field_name, message)
        visit_date = cleaned.get("visit_date")
        if visit_date:
            cleaned["visit_date"] = date.fromisoformat(visit_date)
        return cleaned


class PlanningFilterForm(forms.Form):
    category = forms.ModelChoiceField(
        label="Catégorie", queryset=Category.objects.none(), required=False
    )
    level = forms.ModelChoiceField(
        label="Niveau conseillé", queryset=SchoolLevel.objects.none(), required=False
    )
    starts_at = forms.TimeField(
        label="À partir de", required=False, widget=forms.TimeInput(attrs={"type": "time"})
    )
    available_only = forms.BooleanField(label="Places disponibles uniquement", required=False)
    accessible_only = forms.BooleanField(label="Accessibilité renseignée", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(is_active=True)
        self.fields["level"].queryset = SchoolLevel.objects.filter(is_active=True)

    def apply(self, queryset):
        if not self.is_valid():
            return queryset
        if category := self.cleaned_data.get("category"):
            queryset = queryset.filter(animation__category=category)
        if level := self.cleaned_data.get("level"):
            queryset = queryset.filter(animation__recommended_levels=level)
        if starts_at := self.cleaned_data.get("starts_at"):
            queryset = queryset.filter(starts_at__gte=starts_at)
        if self.cleaned_data.get("available_only"):
            queryset = queryset.filter(_remaining_capacity__gt=0)
        if self.cleaned_data.get("accessible_only"):
            queryset = queryset.exclude(animation__accessibility="")
        return queryset.distinct()


class PlanningForm(forms.Form):
    """Dynamic reservation fields for the sessions currently displayed."""

    def __init__(self, *args, registration, sessions, **kwargs):
        super().__init__(*args, **kwargs)
        self.registration = registration
        self.sessions = list(sessions)
        current = {
            reservation.session_id: (
                reservation.student_count,
                reservation.chaperone_count,
            )
            for reservation in registration.reservations.filter(status="ACTIVE")
        }
        for session in self.sessions:
            current_count, _current_chaperone_count = current.get(session.pk, (0, 0))
            field = forms.IntegerField(
                label=f"Élèves pour {session.animation.title} à {session.starts_at:%H:%M}",
                min_value=0,
                max_value=registration.student_count,
                required=False,
                initial=current_count,
                disabled=session.status != Session.Status.OPEN and current_count == 0,
                widget=forms.NumberInput(
                    attrs={
                        "inputmode": "numeric",
                        "class": "reservation-count",
                        "aria-describedby": f"capacity-{session.pk}",
                    }
                ),
            )
            self.fields[self.field_name(session.pk)] = field

    @staticmethod
    def field_name(session_id):
        return f"session_{session_id}"

    def submitted_counts(self):
        if not self.is_valid():
            raise ValueError("Le formulaire doit être valide.")
        return {
            session.pk: self.cleaned_data.get(self.field_name(session.pk)) or 0
            for session in self.sessions
        }


class RegistrationUpdateForm(forms.ModelForm):
    teacher_first_name = forms.CharField(label="Prénom du professeur", max_length=100)
    teacher_last_name = forms.CharField(label="Nom du professeur", max_length=100)
    teacher_email = forms.EmailField(label="Courriel du professeur")
    teacher_phone = forms.CharField(label="Téléphone du professeur", max_length=30)

    class Meta:
        model = Registration
        fields = (
            "group_name",
            "school_level",
            "student_count",
            "chaperone_count",
            "special_needs",
            "comment",
        )
        widgets = {
            "special_needs": forms.Textarea(attrs={"rows": 3}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.teacher_id:
            self.fields["teacher_first_name"].initial = self.instance.teacher.first_name
            self.fields["teacher_last_name"].initial = self.instance.teacher.last_name
            self.fields["teacher_email"].initial = self.instance.teacher.email
            self.fields["teacher_phone"].initial = self.instance.teacher.phone


class ConfirmationForm(forms.Form):
    confirm = forms.BooleanField(
        label="Je confirme l’exactitude des informations et des effectifs réservés."
    )


class CancellationForm(forms.Form):
    confirm = forms.BooleanField(label="Je confirme l’annulation complète de cette inscription.")
