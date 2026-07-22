from django.db import migrations, models


SCHOOL_LEVELS = (
    ("CAPA_1", "CAP / CAP agricole — 1re année", 10),
    ("CAPA_2", "CAP / CAP agricole — 2e année", 20),
    ("LYC_2DE", "Seconde", 30),
    ("LYC_1ERE", "Première", 40),
    ("LYC_TERM", "Terminale", 50),
    ("BTS_BTSA_1", "BTS / BTSA — 1re année", 60),
    ("BTS_BTSA_2", "BTS / BTSA — 2e année", 70),
    ("SUP_LICENCE_BUT", "Licence / BUT", 80),
    ("SUP_MASTER_ING", "Master / cursus ingénieur", 90),
    ("LYC_AUTRE", "Autre niveau lycée", 100),
    ("SUP_AUTRE", "Autre niveau supérieur", 110),
)

LEGACY_ALIASES = (
    ("2", "seconde", "LYC_2DE", "Seconde", 30),
    ("1", "1 ère", "LYC_1ERE", "Première", 40),
)


def seed_school_levels(apps, schema_editor):
    SchoolLevel = apps.get_model("catalogue", "SchoolLevel")

    # Les deux niveaux déjà utilisés en production sont renommés en place
    # afin de conserver leurs clés primaires et toutes les inscriptions liées.
    for old_code, old_label, code, label, sort_order in LEGACY_ALIASES:
        legacy = SchoolLevel.objects.filter(
            code=old_code,
            label__iexact=old_label,
        ).first()
        if legacy is None:
            continue
        if SchoolLevel.objects.filter(code=code).exclude(pk=legacy.pk).exists():
            legacy.is_active = False
            legacy.save(update_fields=("is_active",))
        else:
            legacy.code = code
            legacy.label = label
            legacy.sort_order = sort_order
            legacy.is_active = True
            legacy.save(
                update_fields=("code", "label", "sort_order", "is_active")
            )

    for code, label, sort_order in SCHOOL_LEVELS:
        SchoolLevel.objects.get_or_create(
            code=code,
            defaults={
                "label": label,
                "sort_order": sort_order,
                "is_active": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("catalogue", "0003_add_organizer_email")]

    operations = [
        migrations.AddField(
            model_name="schoollevel",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="actif"),
        ),
        migrations.RunPython(seed_school_levels, migrations.RunPython.noop),
    ]
