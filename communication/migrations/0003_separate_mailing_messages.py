from django.db import migrations, models


def copy_group_message_to_organizers(apps, schema_editor):
    MailingCampaign = apps.get_model("communication", "MailingCampaign")
    for campaign in MailingCampaign.objects.iterator():
        campaign.organizer_subject = campaign.subject
        campaign.organizer_body_html = campaign.body_html
        campaign.organizer_body_text = campaign.body_text
        campaign.save(
            update_fields=(
                "organizer_subject",
                "organizer_body_html",
                "organizer_body_text",
            )
        )


class Migration(migrations.Migration):
    dependencies = [("communication", "0002_mailingcampaign_mailingdelivery")]

    operations = [
        migrations.AlterField(
            model_name="mailingcampaign",
            name="subject",
            field=models.CharField(
                max_length=255, verbose_name="objet pour les responsables de groupe"
            ),
        ),
        migrations.AlterField(
            model_name="mailingcampaign",
            name="body_html",
            field=models.TextField(
                verbose_name="contenu enrichi pour les responsables de groupe"
            ),
        ),
        migrations.AlterField(
            model_name="mailingcampaign",
            name="body_text",
            field=models.TextField(
                verbose_name="contenu texte pour les responsables de groupe"
            ),
        ),
        migrations.AddField(
            model_name="mailingcampaign",
            name="organizer_subject",
            field=models.CharField(
                blank=True,
                max_length=255,
                verbose_name="objet pour les responsables d’animation",
            ),
        ),
        migrations.AddField(
            model_name="mailingcampaign",
            name="organizer_body_html",
            field=models.TextField(
                blank=True,
                verbose_name="contenu enrichi pour les responsables d’animation",
            ),
        ),
        migrations.AddField(
            model_name="mailingcampaign",
            name="organizer_body_text",
            field=models.TextField(
                blank=True,
                verbose_name="contenu texte pour les responsables d’animation",
            ),
        ),
        migrations.RunPython(copy_group_message_to_organizers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="mailingdelivery",
            name="recipient_kind",
            field=models.CharField(
                choices=[
                    ("TEACHER", "Responsable de groupe"),
                    ("ORGANIZER", "Responsable d’animation"),
                ],
                max_length=10,
                verbose_name="type de destinataire",
            ),
        ),
    ]
