import django.db.models.deletion
from django.db import migrations, models


ANIMATION_THEMES = (
    ("climat", "Climat", 10),
    ("biodiversite", "Biodiversité", 20),
    ("techniques-vegetales", "Techniques végétales", 30),
    ("techniques-animales", "Techniques animales", 40),
    ("sol", "Sol", 50),
    ("eau", "Eau", 60),
    ("desherbage-mecanique", "Désherbage mécanique", 70),
    ("jeu-de-piste", "Jeu de piste", 80),
    ("temoignage", "Témoignage", 90),
    ("conference", "Conférence", 100),
)


def seed_themes_and_copy_categories(apps, schema_editor):
    Animation = apps.get_model("catalogue", "Animation")
    Category = apps.get_model("catalogue", "Category")
    Theme = apps.get_model("catalogue", "Theme")

    for slug, name, sort_order in ANIMATION_THEMES:
        Theme.objects.create(
            slug=slug,
            name=name,
            sort_order=sort_order,
            is_active=True,
        )

    theme_by_category_id = {}
    for category in Category.objects.order_by("pk"):
        theme_by_slug = Theme.objects.filter(slug=category.slug).first()
        theme_by_name = Theme.objects.filter(name__iexact=category.name).first()
        if (
            theme_by_slug is not None
            and theme_by_name is not None
            and theme_by_slug.pk != theme_by_name.pk
        ):
            raise RuntimeError(
                f"Conflit entre la catégorie historique « {category.name} » "
                f"et son slug « {category.slug} »."
            )
        theme = theme_by_slug or theme_by_name
        if theme is None:
            theme = Theme.objects.create(
                name=category.name,
                slug=category.slug,
                is_active=category.is_active and category.slug != "non-classee",
            )
        theme_by_category_id[category.pk] = theme

    for animation in Animation.objects.exclude(category_id=None).iterator():
        theme = theme_by_category_id.get(animation.category_id)
        if theme is not None:
            animation.themes.add(theme)


def restore_legacy_category_links(apps, schema_editor):
    Animation = apps.get_model("catalogue", "Animation")
    Category = apps.get_model("catalogue", "Category")

    if not Animation.objects.filter(category_id=None).exists():
        return
    fallback = Category.objects.filter(slug="non-classee").first()
    if fallback is None:
        fallback = Category.objects.filter(name__iexact="Non classée").first()
    if fallback is None:
        fallback = Category.objects.create(
            name="Non classée",
            slug="non-classee",
            is_active=False,
        )
    Animation.objects.filter(category_id=None).update(category_id=fallback.pk)


class Migration(migrations.Migration):
    dependencies = [("catalogue", "0004_seed_frab_school_levels")]

    operations = [
        migrations.CreateModel(
            name="Theme",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100, unique=True, verbose_name="nom")),
                (
                    "slug",
                    models.SlugField(unique=True, verbose_name="identifiant URL"),
                ),
                (
                    "sort_order",
                    models.PositiveSmallIntegerField(
                        default=0,
                        verbose_name="ordre d’affichage",
                    ),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="active")),
            ],
            options={
                "verbose_name": "thématique",
                "verbose_name_plural": "thématiques",
                "ordering": ("sort_order", "name", "pk"),
            },
        ),
        migrations.AddField(
            model_name="animation",
            name="themes",
            field=models.ManyToManyField(
                related_name="animations",
                to="catalogue.theme",
                verbose_name="thématiques",
            ),
        ),
        migrations.AddField(
            model_name="animation",
            name="venue_category",
            field=models.CharField(
                choices=[("INDOOR", "Salle"), ("OUTDOOR", "Extérieur")],
                max_length=7,
                null=True,
                verbose_name="catégorie",
            ),
        ),
        migrations.AlterField(
            model_name="animation",
            name="category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="animations",
                to="catalogue.category",
                verbose_name="ancienne catégorie",
            ),
        ),
        migrations.AddConstraint(
            model_name="animation",
            constraint=models.CheckConstraint(
                condition=models.Q(venue_category__in=("INDOOR", "OUTDOOR"))
                | models.Q(venue_category__isnull=True),
                name="cat_animation_venue_category_valid",
            ),
        ),
        migrations.RunPython(
            seed_themes_and_copy_categories,
            restore_legacy_category_links,
        ),
    ]
