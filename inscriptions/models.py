import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .choices import DEPARTMENT_CHOICES


class Institution(models.Model):
    class Type(models.TextChoices):
        PRIMARY_SCHOOL = "PRIMARY_SCHOOL", "École primaire"
        MIDDLE_SCHOOL = "MIDDLE_SCHOOL", "Collège"
        HIGH_SCHOOL = "HIGH_SCHOOL", "Lycée"
        AGRICULTURAL = "AGRICULTURAL", "Établissement agricole"
        HIGHER_EDUCATION = "HIGHER_EDUCATION", "Enseignement supérieur"
        OTHER = "OTHER", "Autre"

    name = models.CharField("nom", max_length=200, db_index=True)
    institution_type = models.CharField(
        "type d'établissement", max_length=30, choices=Type.choices, default=Type.OTHER
    )
    address = models.CharField("adresse", max_length=255, blank=True)
    postal_code = models.CharField("code postal", max_length=10, blank=True, db_index=True)
    city = models.CharField("ville", max_length=120, db_index=True)
    department = models.CharField(
        "département",
        max_length=3,
        choices=DEPARTMENT_CHOICES,
        db_index=True,
    )
    phone = models.CharField("téléphone", max_length=30, blank=True)
    administrative_email = models.EmailField("courriel administratif", blank=True)
    created_at = models.DateTimeField("créé le", auto_now_add=True)
    updated_at = models.DateTimeField("modifié le", auto_now=True)

    class Meta:
        ordering = ("name", "postal_code", "city")
        verbose_name = "établissement"
        verbose_name_plural = "établissements"
        indexes = [models.Index(fields=("name", "postal_code"), name="institution_name_postal_idx")]

    def __str__(self):
        return f"{self.name} — {self.city} ({self.postal_code})"


class Teacher(models.Model):
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="teachers",
        verbose_name="établissement",
    )
    first_name = models.CharField("prénom", max_length=100)
    last_name = models.CharField("nom", max_length=100)
    email = models.EmailField("courriel", db_index=True)
    phone = models.CharField("téléphone", max_length=30)
    created_at = models.DateTimeField("créé le", auto_now_add=True)
    updated_at = models.DateTimeField("modifié le", auto_now=True)

    class Meta:
        ordering = ("last_name", "first_name", "email")
        verbose_name = "professeur"
        verbose_name_plural = "professeurs"

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class GroupFamily(models.Model):
    name = models.CharField("nom", max_length=100, unique=True)
    slug = models.SlugField("identifiant", max_length=120, unique=True)
    is_active = models.BooleanField("active", default=True)
    sort_order = models.PositiveSmallIntegerField("ordre d'affichage", default=0)

    class Meta:
        ordering = ("sort_order", "name", "pk")
        verbose_name = "famille de groupe"
        verbose_name_plural = "familles de groupes"

    def __str__(self):
        return self.name


class Registration(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Brouillon"
        CONFIRMED = "CONFIRMED", "Confirmée"
        CANCELLED = "CANCELLED", "Annulée"

    reference = models.UUIDField(
        "référence", default=uuid.uuid4, unique=True, editable=False, db_index=True
    )
    group_code = models.CharField(
        "code du groupe",
        max_length=80,
        unique=True,
        editable=False,
        blank=True,
        db_index=True,
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="registrations",
        verbose_name="établissement",
    )
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.PROTECT,
        related_name="registrations",
        verbose_name="professeur",
    )
    group_name = models.CharField("nom du groupe", max_length=150)
    family = models.ForeignKey(
        GroupFamily,
        on_delete=models.PROTECT,
        related_name="registrations",
        verbose_name="famille",
        null=True,
        blank=True,
    )
    school_level = models.ForeignKey(
        "catalogue.SchoolLevel", on_delete=models.PROTECT, verbose_name="niveau scolaire"
    )
    student_count = models.PositiveSmallIntegerField("nombre d'élèves")
    chaperone_count = models.PositiveSmallIntegerField("nombre d'accompagnateurs", default=0)
    visit_date = models.DateField("jour de visite", db_index=True)
    special_needs = models.TextField("besoins particuliers", blank=True)
    level_comment = models.TextField("remarque sur le niveau", blank=True)
    comment = models.TextField("commentaire", blank=True)
    status = models.CharField(
        "statut", max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    draft_expires_at = models.DateTimeField(
        "expiration du brouillon", null=True, blank=True, db_index=True
    )
    edit_token_digest = models.CharField(
        "empreinte du jeton de modification", max_length=64, unique=True, editable=False
    )
    token_created_at = models.DateTimeField("jeton créé le", default=timezone.now)
    token_revoked_at = models.DateTimeField("jeton révoqué le", null=True, blank=True)
    confirmed_at = models.DateTimeField("confirmée le", null=True, blank=True)
    cancelled_at = models.DateTimeField("annulée le", null=True, blank=True)
    anonymized_at = models.DateTimeField("anonymisée le", null=True, blank=True, db_index=True)
    created_at = models.DateTimeField("créée le", auto_now_add=True)
    updated_at = models.DateTimeField("modifiée le", auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "inscription"
        verbose_name_plural = "inscriptions"
        constraints = [
            models.CheckConstraint(
                condition=Q(student_count__gt=0), name="registration_students_positive"
            ),
            models.CheckConstraint(
                condition=Q(chaperone_count__gte=0), name="registration_chaperones_nonnegative"
            ),
        ]

    def __str__(self):
        label = self.group_name or self.group_code
        return f"{label} — {self.reference}"

    def save(self, *args, **kwargs):
        from .codes import generate_unique_group_code, normalize_group_code

        normalized_code = normalize_group_code(self.group_code)
        if not normalized_code:
            normalized_code = generate_unique_group_code(excluding_registration_id=self.pk)
        code_changed = normalized_code != self.group_code
        self.group_code = normalized_code
        if code_changed and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = tuple({*kwargs["update_fields"], "group_code"})
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        errors = {}
        if self.teacher_id and self.institution_id:
            teacher_institution_id = getattr(self.teacher, "institution_id", None)
            if teacher_institution_id != self.institution_id:
                errors["teacher"] = "Le professeur doit appartenir à l'établissement choisi."
        if self.status == self.Status.DRAFT and self.draft_expires_at is None:
            errors["draft_expires_at"] = "Un brouillon doit avoir une date d'expiration."
        if self.status != self.Status.DRAFT and self.draft_expires_at is not None:
            errors["draft_expires_at"] = (
                "La date d'expiration est réservée aux inscriptions en brouillon."
            )
        if errors:
            raise ValidationError(errors)

    @property
    def is_draft_hold_active(self):
        return (
            self.status == self.Status.DRAFT
            and self.draft_expires_at is not None
            and self.draft_expires_at > timezone.now()
        )

    @property
    def total_participant_count(self):
        return self.student_count + self.chaperone_count


class Reservation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CANCELLED = "CANCELLED", "Annulée"

    registration = models.ForeignKey(
        Registration,
        on_delete=models.CASCADE,
        related_name="reservations",
        verbose_name="inscription",
    )
    session = models.ForeignKey(
        "catalogue.Session",
        on_delete=models.PROTECT,
        related_name="reservations",
        verbose_name="séance",
    )
    student_count = models.PositiveSmallIntegerField("nombre d'élèves")
    chaperone_count = models.PositiveSmallIntegerField("nombre d'accompagnateurs", default=0)
    status = models.CharField(
        "statut", max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    cancelled_at = models.DateTimeField("annulée le", null=True, blank=True)
    created_at = models.DateTimeField("créée le", auto_now_add=True)
    updated_at = models.DateTimeField("modifiée le", auto_now=True)

    class Meta:
        ordering = ("session__date", "session__starts_at", "pk")
        verbose_name = "réservation"
        verbose_name_plural = "réservations"
        constraints = [
            models.CheckConstraint(
                condition=Q(student_count__gt=0), name="reservation_students_positive"
            ),
            models.CheckConstraint(
                condition=Q(chaperone_count__gte=0), name="reservation_chaperones_nonnegative"
            ),
            models.UniqueConstraint(
                fields=("registration", "session"),
                condition=Q(status="ACTIVE"),
                name="one_active_reservation_per_session",
            ),
        ]

    def __str__(self):
        return f"{self.registration} — {self.session} ({self.student_count})"

    @property
    def total_participant_count(self):
        return self.student_count + self.chaperone_count

    def clean(self):
        super().clean()
        errors = {}
        if self.cancelled_at and self.status != self.Status.CANCELLED:
            errors["cancelled_at"] = "Seule une réservation annulée peut être horodatée."
        if self.status == self.Status.CANCELLED and self.cancelled_at is None:
            errors["cancelled_at"] = "Une réservation annulée doit être horodatée."
        if self.registration_id and self.session_id:
            registration_date = getattr(self.registration, "visit_date", None)
            session_date = getattr(self.session, "date", None)
            if registration_date != session_date:
                errors["session"] = "La séance doit avoir lieu le jour de l'inscription."
        if errors:
            raise ValidationError(errors)


class RegistrationEvent(models.Model):
    class Type(models.TextChoices):
        CREATED = "CREATED", "Création"
        CONFIRMED = "CONFIRMED", "Confirmation"
        UPDATED = "UPDATED", "Modification"
        CANCELLED = "CANCELLED", "Annulation"
        TOKEN_ROTATED = "TOKEN_ROTATED", "Renouvellement du lien"
        EMAIL_SENT = "EMAIL_SENT", "Courriel envoyé"
        EMAIL_FAILED = "EMAIL_FAILED", "Échec du courriel"
        ANONYMIZED = "ANONYMIZED", "Anonymisation"

    class ActorKind(models.TextChoices):
        TEACHER = "TEACHER", "Professeur"
        STAFF = "STAFF", "Équipe"
        SYSTEM = "SYSTEM", "Système"

    registration = models.ForeignKey(
        Registration,
        on_delete=models.PROTECT,
        related_name="events",
        verbose_name="inscription",
    )
    event_type = models.CharField("type", max_length=20, choices=Type.choices)
    actor_kind = models.CharField("acteur", max_length=10, choices=ActorKind.choices)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="registration_events",
        null=True,
        blank=True,
        verbose_name="utilisateur",
    )
    changes = models.JSONField("modifications", default=dict, blank=True)
    created_at = models.DateTimeField("créé le", auto_now_add=True)

    class Meta:
        ordering = ("created_at", "pk")
        verbose_name = "événement d'inscription"
        verbose_name_plural = "événements d'inscription"
        indexes = [models.Index(fields=("registration", "created_at"))]

    def __str__(self):
        return f"{self.registration.reference} — {self.get_event_type_display()}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("Un événement d'audit ne peut pas être modifié.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Un événement d'audit ne peut pas être supprimé.")
