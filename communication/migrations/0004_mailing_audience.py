from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("communication", "0003_separate_mailing_messages")]

    operations = [
        migrations.AlterField(
            model_name="mailingcampaign",
            name="subject",
            field=models.CharField(
                blank=True,
                max_length=255,
                verbose_name="objet pour les responsables de groupe",
            ),
        ),
        migrations.AlterField(
            model_name="mailingcampaign",
            name="body_html",
            field=models.TextField(
                blank=True,
                verbose_name="contenu enrichi pour les responsables de groupe",
            ),
        ),
        migrations.AlterField(
            model_name="mailingcampaign",
            name="body_text",
            field=models.TextField(
                blank=True,
                verbose_name="contenu texte pour les responsables de groupe",
            ),
        ),
        migrations.AddField(
            model_name="mailingcampaign",
            name="audience",
            field=models.CharField(
                choices=[
                    ("GROUPS", "Responsables de groupe"),
                    ("ORGANIZERS", "Responsables d’animation"),
                    ("BOTH", "Les deux publics"),
                ],
                db_index=True,
                default="BOTH",
                max_length=10,
                verbose_name="public",
            ),
        ),
    ]
