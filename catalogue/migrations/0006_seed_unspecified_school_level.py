from django.db import migrations


def seed_unspecified_school_level(apps, schema_editor):
    SchoolLevel = apps.get_model("catalogue", "SchoolLevel")
    SchoolLevel.objects.get_or_create(
        code="NON_RENSEIGNE",
        defaults={
            "label": "Non renseigné",
            "sort_order": 999,
            "is_active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("catalogue", "0005_animation_taxonomy")]

    operations = [
        migrations.RunPython(
            seed_unspecified_school_level,
            migrations.RunPython.noop,
        )
    ]
