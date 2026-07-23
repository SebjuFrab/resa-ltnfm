from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q, Sum, Value
from django.db.models.functions import Coalesce, Greatest, Lower
from django.utils import timezone


class Category(models.Model):
    name = models.CharField("nom", max_length=100, unique=True)
    slug = models.SlugField("identifiant URL", unique=True)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        ordering = ("name",)
        verbose_name = "catégorie"
        verbose_name_plural = "catégories"

    def __str__(self):
        return self.name


class Theme(models.Model):
    name = models.CharField("nom", max_length=100, unique=True)
    slug = models.SlugField("identifiant URL", unique=True)
    sort_order = models.PositiveSmallIntegerField("ordre d’affichage", default=0)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        ordering = ("sort_order", "name", "pk")
        verbose_name = "thématique"
        verbose_name_plural = "thématiques"

    def __str__(self):
        return self.name


class SchoolLevel(models.Model):
    code = models.CharField("code", max_length=30, unique=True)
    label = models.CharField("libellé", max_length=100)
    sort_order = models.PositiveSmallIntegerField("ordre d’affichage", default=0)
    is_active = models.BooleanField("actif", default=True)

    class Meta:
        ordering = ("sort_order", "label", "code")
        verbose_name = "niveau scolaire"
        verbose_name_plural = "niveaux scolaires"

    def __str__(self):
        return self.label


class Animation(models.Model):
    class VenueCategory(models.TextChoices):
        INDOOR = "INDOOR", "Salle"
        OUTDOOR = "OUTDOOR", "Extérieur"

    title = models.CharField("titre", max_length=200)
    slug = models.SlugField("identifiant URL", max_length=220, unique=True)
    short_description = models.CharField("description courte", max_length=300)
    description = models.TextField("description", blank=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="animations",
        verbose_name="ancienne catégorie",
        blank=True,
        null=True,
    )
    venue_category = models.CharField(
        "catégorie",
        max_length=7,
        choices=VenueCategory.choices,
        null=True,
    )
    recommended_levels = models.ManyToManyField(
        SchoolLevel,
        related_name="animations",
        verbose_name="niveaux conseillés",
        blank=True,
    )
    themes = models.ManyToManyField(
        Theme,
        related_name="animations",
        verbose_name="thématiques",
    )
    indicative_duration = models.PositiveSmallIntegerField(
        "durée indicative (minutes)",
        validators=(MinValueValidator(1),),
    )
    image = models.ImageField("image", upload_to="animations/", blank=True)
    instructions = models.TextField("consignes", blank=True)
    accessibility = models.TextField("accessibilité", blank=True)
    is_active = models.BooleanField("active", default=True)
    created_at = models.DateTimeField("créée le", auto_now_add=True)
    updated_at = models.DateTimeField("modifiée le", auto_now=True)

    class Meta:
        ordering = ("title", "pk")
        verbose_name = "animation"
        verbose_name_plural = "animations"
        constraints = (
            models.CheckConstraint(
                condition=Q(indicative_duration__gt=0),
                name="cat_animation_duration_positive",
            ),
            models.CheckConstraint(
                condition=Q(venue_category__in=("INDOOR", "OUTDOOR"))
                | Q(venue_category__isnull=True),
                name="cat_animation_venue_category_valid",
            ),
        )

    def __str__(self):
        return self.title


class SessionQuerySet(models.QuerySet):
    def with_capacities(self, *, at=None):
        """Ajoute les capacités occupée et restante sans requête par séance."""
        at = at or timezone.now()
        active_holds = Q(reservations__status="ACTIVE") & (
            Q(reservations__registration__status="CONFIRMED")
            | Q(
                reservations__registration__status="DRAFT",
                reservations__registration__draft_expires_at__gt=at,
            )
        )
        reserved = Coalesce(
            Sum(
                F("reservations__student_count") + F("reservations__chaperone_count"),
                filter=active_holds,
                output_field=models.IntegerField(),
            ),
            Value(0),
            output_field=models.IntegerField(),
        )
        return self.annotate(_reserved_capacity=reserved).annotate(
            _remaining_capacity=Greatest(
                F("max_capacity") - F("_reserved_capacity"),
                Value(0),
                output_field=models.IntegerField(),
            )
        )


class Session(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Ouverte"
        CLOSED = "CLOSED", "Fermée"
        CANCELLED = "CANCELLED", "Annulée"

    animation = models.ForeignKey(
        Animation,
        on_delete=models.PROTECT,
        related_name="sessions",
        verbose_name="animation",
    )
    date = models.DateField("date", db_index=True)
    starts_at = models.TimeField("heure de début")
    ends_at = models.TimeField("heure de fin")
    location = models.CharField("lieu", max_length=200)
    max_capacity = models.PositiveIntegerField(
        "capacité maximale",
        validators=(MinValueValidator(1),),
    )
    status = models.CharField(
        "statut",
        max_length=10,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    organizer = models.CharField("organisateur", max_length=200, blank=True)
    organizer_email = models.EmailField("courriel du responsable", blank=True)
    internal_comment = models.TextField("commentaire interne", blank=True)
    created_at = models.DateTimeField("créée le", auto_now_add=True)
    updated_at = models.DateTimeField("modifiée le", auto_now=True)

    objects = SessionQuerySet.as_manager()

    class Meta:
        ordering = ("date", "starts_at", "ends_at", "animation__title", "pk")
        verbose_name = "séance"
        verbose_name_plural = "séances"
        constraints = (
            models.CheckConstraint(
                condition=Q(max_capacity__gt=0),
                name="cat_session_capacity_positive",
            ),
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")),
                name="cat_session_end_after_start",
            ),
            models.UniqueConstraint(
                "animation",
                "date",
                "starts_at",
                "ends_at",
                Lower("location"),
                name="cat_session_unique_schedule_location",
            ),
        )
        indexes = (
            models.Index(fields=("date", "starts_at"), name="cat_session_date_start_idx"),
            models.Index(fields=("date", "status"), name="cat_session_date_status_idx"),
        )

    def __str__(self):
        if not self.date or not self.starts_at:
            return str(self.animation)
        return f"{self.animation} — {self.date:%d/%m/%Y} à {self.starts_at:%H:%M}"

    def _calculate_reserved_capacity(self, *, at=None):
        if not self.pk:
            return 0
        at = at or timezone.now()
        return (
            self.reservations.filter(status="ACTIVE")
            .filter(
                Q(registration__status="CONFIRMED")
                | Q(
                    registration__status="DRAFT",
                    registration__draft_expires_at__gt=at,
                )
            )
            .aggregate(
                total=Coalesce(
                    Sum(
                        F("student_count") + F("chaperone_count"),
                        output_field=models.IntegerField(),
                    ),
                    Value(0),
                    output_field=models.IntegerField(),
                )
            )["total"]
        )

    @property
    def reserved_capacity(self):
        if hasattr(self, "_reserved_capacity"):
            return self._reserved_capacity
        return self._calculate_reserved_capacity()

    @property
    def remaining_capacity(self):
        if hasattr(self, "_remaining_capacity"):
            return self._remaining_capacity
        return max(0, self.max_capacity - self.reserved_capacity)

    @property
    def is_bookable(self):
        return (
            self.status == self.Status.OPEN
            and self.animation.is_active
            and self.remaining_capacity > 0
        )
