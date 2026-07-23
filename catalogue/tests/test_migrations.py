from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class AnimationTaxonomyMigrationTests(TransactionTestCase):
    migrate_from = [("catalogue", "0004_seed_frab_school_levels")]
    migrate_to = [("catalogue", "0005_animation_taxonomy")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        Category = old_apps.get_model("catalogue", "Category")
        Animation = old_apps.get_model("catalogue", "Animation")
        sol = Category.objects.create(name="Sol", slug="sol")
        unclassified = Category.objects.create(
            name="Non classée",
            slug="non-classee",
        )
        legacy_climate = Category.objects.create(name="Climat", slug="climate")
        self.animation_ids = {
            "sol": Animation.objects.create(
                title="Animation sol",
                slug="animation-sol",
                short_description="Sol",
                category=sol,
                indicative_duration=45,
            ).pk,
            "unclassified": Animation.objects.create(
                title="Animation à classer",
                slug="animation-a-classer",
                short_description="À classer",
                category=unclassified,
                indicative_duration=45,
            ).pk,
            "climate": Animation.objects.create(
                title="Animation climat",
                slug="animation-climat",
                short_description="Climat",
                category=legacy_climate,
                indicative_duration=45,
            ).pk,
        }

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_legacy_categories_are_preserved_as_themes(self):
        Animation = self.apps.get_model("catalogue", "Animation")
        Theme = self.apps.get_model("catalogue", "Theme")

        self.assertEqual(
            Theme.objects.filter(
                slug__in=(
                    "climat",
                    "biodiversite",
                    "techniques-vegetales",
                    "techniques-animales",
                    "sol",
                    "eau",
                    "desherbage-mecanique",
                    "jeu-de-piste",
                    "temoignage",
                    "conference",
                )
            ).count(),
            10,
        )
        self.assertEqual(
            set(
                Animation.objects.get(pk=self.animation_ids["sol"])
                .themes.values_list("slug", flat=True)
            ),
            {"sol"},
        )
        self.assertEqual(
            set(
                Animation.objects.get(pk=self.animation_ids["climate"])
                .themes.values_list("slug", flat=True)
            ),
            {"climat"},
        )
        self.assertFalse(Theme.objects.get(slug="non-classee").is_active)
        self.assertIsNone(
            Animation.objects.get(
                pk=self.animation_ids["unclassified"]
            ).venue_category
        )

    def test_reverse_assigns_a_fallback_legacy_category_to_new_animations(self):
        Animation = self.apps.get_model("catalogue", "Animation")
        Theme = self.apps.get_model("catalogue", "Theme")
        animation = Animation.objects.create(
            title="Nouvelle animation",
            slug="nouvelle-animation",
            short_description="Nouvelle",
            venue_category="INDOOR",
            indicative_duration=45,
        )
        animation.themes.add(Theme.objects.get(slug="climat"))

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldAnimation = old_apps.get_model("catalogue", "Animation")

        self.assertEqual(
            OldAnimation.objects.get(pk=animation.pk).category.slug,
            "non-classee",
        )
