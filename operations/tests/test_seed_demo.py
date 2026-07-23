from django.core.management import call_command
from django.test import TestCase

from catalogue.models import Animation, SchoolLevel, Session, Theme
from inscriptions.models import GroupFamily, Registration


class SeedDemoTests(TestCase):
    def test_command_is_idempotent(self):
        call_command("seed_demo", verbosity=0)
        counts = (
            Animation.objects.count(),
            Session.objects.count(),
            Registration.objects.count(),
        )

        call_command("seed_demo", verbosity=0)

        self.assertEqual(
            (
                Animation.objects.count(),
                Session.objects.count(),
                Registration.objects.count(),
            ),
            counts,
        )
        self.assertFalse(
            SchoolLevel.objects.filter(code__in=("COLLEGE", "LYCEE")).exists()
        )
        self.assertFalse(GroupFamily.objects.filter(slug="scolaires").exists())
        self.assertEqual(
            Animation.objects.get(slug="vie-du-sol").venue_category,
            Animation.VenueCategory.OUTDOOR,
        )
        self.assertEqual(
            set(
                Animation.objects.get(slug="agriculture-et-climat")
                .themes.values_list("slug", flat=True)
            ),
            {"climat", "conference"},
        )
        self.assertTrue(Theme.objects.filter(slug="desherbage-mecanique").exists())
