from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("communication", "0004_mailing_audience")]

    operations = [
        migrations.AddField(
            model_name="mailingcampaign",
            name="recipient_selection",
            field=models.JSONField(
                blank=True,
                default=None,
                null=True,
                verbose_name="destinataires sélectionnés manuellement",
            ),
        ),
    ]
