from django.db import migrations, models


GROUP_FAMILIES = (
    ("lycee-agricole", "Lycée agricole", 10),
    ("lycee-professionnel", "Lycée professionnel", 20),
    (
        "lycee-general-technologique",
        "Lycée général et technologique",
        30,
    ),
    ("enseignement-superieur", "Enseignement supérieur", 40),
    ("autre-public", "Autre / groupe mixte", 900),
)


def seed_group_families(apps, schema_editor):
    GroupFamily = apps.get_model("inscriptions", "GroupFamily")
    GroupFamily.objects.filter(slug="non-renseignee").update(is_active=False)
    for slug, name, sort_order in GROUP_FAMILIES:
        existing = GroupFamily.objects.filter(slug=slug).first()
        if existing is None:
            existing = GroupFamily.objects.filter(name=name).first()
        if existing is None:
            GroupFamily.objects.create(
                slug=slug,
                name=name,
                sort_order=sort_order,
            )


class Migration(migrations.Migration):
    dependencies = [("inscriptions", "0003_add_group_fields")]

    operations = [
        migrations.AlterField(
            model_name="institution",
            name="department",
            field=models.CharField(
                choices=[
                    (
                        "Bretagne",
                        [
                            ("22", "22 — Côtes-d'Armor"),
                            ("29", "29 — Finistère"),
                            ("35", "35 — Ille-et-Vilaine"),
                            ("56", "56 — Morbihan"),
                        ],
                    ),
                    (
                        "Départements limitrophes",
                        [
                            ("44", "44 — Loire-Atlantique"),
                            ("49", "49 — Maine-et-Loire"),
                            ("50", "50 — Manche"),
                            ("53", "53 — Mayenne"),
                        ],
                    ),
                ],
                db_index=True,
                max_length=3,
                verbose_name="département",
            ),
        ),
        migrations.AlterField(
            model_name="institution",
            name="institution_type",
            field=models.CharField(
                choices=[
                    ("PRIMARY_SCHOOL", "École primaire"),
                    ("MIDDLE_SCHOOL", "Collège"),
                    ("HIGH_SCHOOL", "Lycée"),
                    ("AGRICULTURAL", "Établissement agricole"),
                    ("HIGHER_EDUCATION", "Enseignement supérieur"),
                    ("OTHER", "Autre"),
                ],
                default="OTHER",
                max_length=30,
                verbose_name="type d'établissement",
            ),
        ),
        migrations.RunPython(seed_group_families, migrations.RunPython.noop),
    ]
