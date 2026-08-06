import uuid

from django.conf import settings
from django.db import models


class EmailLog(models.Model):
    class Kind(models.TextChoices):
        CONFIRMATION = "CONFIRMATION", "Confirmation"
        MODIFICATION = "MODIFICATION", "Modification"
        CANCELLATION = "CANCELLATION", "Annulation"

    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        SENT = "SENT", "Envoyé"
        FAILED = "FAILED", "Échec"

    registration = models.ForeignKey(
        "inscriptions.Registration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="email_logs",
        verbose_name="inscription",
    )
    kind = models.CharField("type", max_length=20, choices=Kind.choices)
    recipient = models.EmailField("destinataire")
    status = models.CharField(
        "statut", max_length=10, choices=Status.choices, default=Status.PENDING
    )
    provider_message_id = models.CharField(
        "identifiant fournisseur", max_length=255, blank=True
    )
    error_summary = models.TextField("erreur", blank=True)
    created_at = models.DateTimeField("créé le", auto_now_add=True)
    sent_at = models.DateTimeField("envoyé le", null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "courriel envoyé"
        verbose_name_plural = "courriels envoyés"

    def __str__(self):
        return f"{self.get_kind_display()} — {self.recipient}"


class MailingCampaign(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Brouillon"
        SENDING = "SENDING", "En cours d’envoi"
        SENT = "SENT", "Envoyé"
        PARTIAL = "PARTIAL", "Partiellement envoyé"
        FAILED = "FAILED", "Échec"

    reference = models.UUIDField(
        "référence", default=uuid.uuid4, unique=True, editable=False
    )
    idempotency_key = models.CharField(
        "clé d’idempotence", max_length=100, unique=True, null=True, blank=True
    )
    subject = models.CharField("objet pour les responsables de groupe", max_length=255)
    body_html = models.TextField("contenu enrichi pour les responsables de groupe")
    body_text = models.TextField("contenu texte pour les responsables de groupe")
    organizer_subject = models.CharField(
        "objet pour les responsables d’animation", max_length=255, blank=True
    )
    organizer_body_html = models.TextField(
        "contenu enrichi pour les responsables d’animation", blank=True
    )
    organizer_body_text = models.TextField(
        "contenu texte pour les responsables d’animation", blank=True
    )
    visit_date = models.DateField("jour filtré", null=True, blank=True, db_index=True)
    family_filter = models.CharField(
        "identifiant de famille filtrée", max_length=100, blank=True
    )
    family_label = models.CharField("famille filtrée", max_length=150, blank=True)
    status = models.CharField(
        "statut", max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="mailing_campaigns_created",
        null=True,
        blank=True,
        verbose_name="créé par",
    )
    created_at = models.DateTimeField("créé le", auto_now_add=True)
    started_at = models.DateTimeField("envoi commencé le", null=True, blank=True)
    completed_at = models.DateTimeField("envoi terminé le", null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "campagne de publipostage"
        verbose_name_plural = "campagnes de publipostage"
        permissions = (("send_mailing", "Peut envoyer un publipostage"),)

    def __str__(self):
        return f"{self.subject} — {self.reference}"

    @property
    def sent_count(self):
        return self.deliveries.filter(status=MailingDelivery.Status.SENT).count()

    @property
    def failed_count(self):
        return self.deliveries.filter(status=MailingDelivery.Status.FAILED).count()


class MailingDelivery(models.Model):
    class RecipientKind(models.TextChoices):
        TEACHER = "TEACHER", "Responsable de groupe"
        ORGANIZER = "ORGANIZER", "Responsable d’animation"

    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        SENDING = "SENDING", "En cours"
        SENT = "SENT", "Envoyé"
        FAILED = "FAILED", "Échec"

    campaign = models.ForeignKey(
        MailingCampaign,
        on_delete=models.PROTECT,
        related_name="deliveries",
        verbose_name="campagne",
    )
    recipient_kind = models.CharField(
        "type de destinataire", max_length=10, choices=RecipientKind.choices
    )
    recipient = models.EmailField("destinataire")
    recipient_name = models.CharField("nom du destinataire", max_length=200, blank=True)
    registration = models.ForeignKey(
        "inscriptions.Registration",
        on_delete=models.SET_NULL,
        related_name="mailing_deliveries",
        null=True,
        blank=True,
        verbose_name="inscription",
    )
    dedupe_key = models.CharField("clé de dédoublonnage", max_length=255)
    context_snapshot = models.JSONField("contexte figé", default=dict)
    status = models.CharField(
        "statut", max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    attempts = models.PositiveSmallIntegerField("tentatives", default=0)
    error_summary = models.TextField("erreur", blank=True)
    created_at = models.DateTimeField("créé le", auto_now_add=True)
    last_attempted_at = models.DateTimeField(
        "dernière tentative le", null=True, blank=True
    )
    sent_at = models.DateTimeField("envoyé le", null=True, blank=True)

    class Meta:
        ordering = ("campaign_id", "recipient_kind", "recipient", "pk")
        verbose_name = "envoi de publipostage"
        verbose_name_plural = "envois de publipostage"
        constraints = (
            models.UniqueConstraint(
                fields=("campaign", "dedupe_key"),
                name="comm_mailing_delivery_dedupe",
            ),
        )
        indexes = (
            models.Index(
                fields=("campaign", "status"), name="comm_delivery_campaign_st_idx"
            ),
        )

    def __str__(self):
        return f"{self.get_recipient_kind_display()} — {self.recipient}"
