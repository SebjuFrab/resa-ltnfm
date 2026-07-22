from django.core.management import call_command
from django.test import TestCase

from catalogue.models import Animation, SchoolLevel, Session
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
