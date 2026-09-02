import csv
from io import StringIO
from unittest.mock import patch

from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from catalogue.models import Animation, Category, Session, Theme
from operations.imports import (
    SessionImportError,
    import_session_payload,
    preview_session_csv,
)


@override_settings(EVENT_DATES=("2026-09-23", "2026-09-24"))
class SessionImportTests(TestCase):
    HEADER = (
        "titre_animation;categorie;thematiques;lieu_de_rendez_vous;"
        "duree;jauge;jour;horaires;"
        "responsable;email_responsable\n"
    )

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name="Nature", slug="nature")
        cls.animation = Animation.objects.create(
            title="Le sol vivant",
            slug="sol-vivant",
            short_description="Découvrir le sol.",
            category=cls.category,
            venue_category=Animation.VenueCategory.INDOOR,
            indicative_duration=45,
        )
        cls.theme = Theme.objects.get(slug="sol")
        cls.animation.themes.add(cls.theme)

    def _upload(self, rows, *, header=None, encoding="utf-8", name="animations.csv"):
        content = (header or self.HEADER) + rows
        return SimpleUploadedFile(
            name,
            content.encode(encoding),
            content_type="text/csv",
        )

    def test_one_line_expands_times_then_confirmation_creates_everything(self):
        preview = preview_session_csv(
            self._upload(
                "Le compost;Extérieur;Sol|Biodiversité;Accueil du hall;"
                "1h;32;Mercredi;09:00, 10:30;"
                "Alice Martin;alice@example.test\n"
            )
        )

        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(len(preview.rows), 2)
        self.assertEqual(
            [(row.starts_at.isoformat(), row.ends_at.isoformat()) for row in preview.rows],
            [("09:00:00", "10:00:00"), ("10:30:00", "11:30:00")],
        )
        self.assertEqual({row.date.isoformat() for row in preview.rows}, {"2026-09-23"})
        self.assertEqual(Animation.objects.count(), 1)
        self.assertEqual(Category.objects.count(), 1)
        self.assertEqual(Session.objects.count(), 0)

        created = import_session_payload([row.as_payload() for row in preview.rows])

        self.assertEqual(len(created), 2)
        animation = Animation.objects.get(title="Le compost")
        self.assertEqual(animation.indicative_duration, 60)
        self.assertIsNone(animation.category)
        self.assertEqual(
            animation.venue_category,
            Animation.VenueCategory.OUTDOOR,
        )
        self.assertEqual(
            set(animation.themes.values_list("slug", flat=True)),
            {"sol", "biodiversite"},
        )
        sessions = list(Session.objects.filter(animation=animation).order_by("starts_at"))
        self.assertEqual([session.max_capacity for session in sessions], [32, 32])
        self.assertEqual([session.organizer for session in sessions], ["Alice Martin"] * 2)
        self.assertEqual(
            [session.organizer_email for session in sessions],
            ["alice@example.test"] * 2,
        )
        self.assertTrue(all(session.status == Session.Status.OPEN for session in sessions))

    def test_existing_animation_is_reused_without_creating_default_category(self):
        preview = preview_session_csv(
            self._upload("Le sol vivant;Salle;Sol;Pôle sols;45;30;jeudi;14:00;Équipe LTNM;\n")
        )

        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(preview.rows[0].animation_id, self.animation.pk)

        import_session_payload([preview.rows[0].as_payload()])

        self.assertEqual(Animation.objects.count(), 1)
        self.assertFalse(Category.objects.filter(slug="non-classee").exists())
        self.assertEqual(Session.objects.get().animation, self.animation)

    def test_human_headers_aliases_and_cp1252_are_accepted(self):
        header = (
            "Titre animation;Catégorie;Thématique;Lieu de RDV;Durée (min);"
            "Capacité;Date;Heure de début;Organisateur;Courriel responsable\n"
        )
        preview = preview_session_csv(
            self._upload(
                "Le sol vivant;Salle;Sol;Pôle sols;45;30;24/09/2026;14h;"
                "Équipe LTNM;contact@example.test\n",
                header=header,
                encoding="cp1252",
            )
        )

        self.assertTrue(preview.is_valid, preview.issues)
        row = preview.rows[0]
        self.assertEqual(row.date.isoformat(), "2026-09-24")
        self.assertEqual(row.starts_at.isoformat(), "14:00:00")
        self.assertEqual(row.organizer_email, "contact@example.test")

    def test_existing_animation_duration_mismatch_is_reported(self):
        preview = preview_session_csv(
            self._upload("Le sol vivant;Salle;Sol;Pôle sols;60;30;mercredi;10:00;;\n")
        )

        self.assertFalse(preview.is_valid)
        self.assertEqual(len(preview.issues), 1)
        self.assertIn("45 minutes", preview.issues[0].message)
        self.assertEqual(Session.objects.count(), 0)

    def test_provided_category_and_themes_must_be_known(self):
        invalid_category = preview_session_csv(
            self._upload("Animation A;Chapiteau;Sol;Salle A;45;30;mercredi;10:00;;\n")
        )
        invalid_theme = preview_session_csv(
            self._upload("Animation B;Salle;Thème inconnu;Salle B;45;30;mercredi;11:00;;\n")
        )
        missing_theme = preview_session_csv(
            self._upload("Animation C;Extérieur;;Pôle C;45;30;mercredi;12:00;;\n")
        )

        self.assertIn("Salle", invalid_category.issues[0].message)
        self.assertIn("inconnue ou inactive", invalid_theme.issues[0].message)
        self.assertTrue(missing_theme.is_valid, missing_theme.issues)
        self.assertEqual(missing_theme.rows[0].theme_ids, ())

    def test_only_title_column_uses_safe_defaults_and_can_be_imported(self):
        preview = preview_session_csv(
            self._upload(
                "Animation minimale\n",
                header="titre_animation\n",
            )
        )

        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(len(preview.rows), 1)
        row = preview.rows[0]
        self.assertEqual(row.animation_title, "Animation minimale")
        self.assertEqual(row.venue_category, Animation.VenueCategory.INDOOR)
        self.assertEqual(row.theme_ids, ())
        self.assertEqual(row.location, "À compléter")
        self.assertEqual(row.duration_minutes, 60)
        self.assertEqual(row.max_capacity, 30)
        self.assertEqual(row.date.isoformat(), "2026-09-23")
        self.assertEqual(row.starts_at.isoformat(), "09:00:00")
        self.assertEqual(row.ends_at.isoformat(), "10:00:00")

        sessions = import_session_payload([row.as_payload()])

        self.assertEqual(len(sessions), 1)
        animation = Animation.objects.get(title="Animation minimale")
        self.assertEqual(animation.themes.count(), 0)
        self.assertEqual(Session.objects.get().animation, animation)

    def test_title_only_preserves_existing_animation_information(self):
        preview = preview_session_csv(self._upload("Le sol vivant\n", header="titre_animation\n"))

        self.assertTrue(preview.is_valid, preview.issues)
        row = preview.rows[0]
        self.assertEqual(row.animation_id, self.animation.pk)
        self.assertEqual(row.duration_minutes, 45)
        self.assertEqual(row.venue_category, Animation.VenueCategory.INDOOR)
        self.assertEqual(row.theme_slugs, ("sol",))

    def test_any_recognized_column_can_create_a_placeholder_animation(self):
        preview = preview_session_csv(self._upload("Alice Martin\n", header="responsable\n"))

        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(preview.rows[0].organizer, "Alice Martin")
        self.assertEqual(
            preview.rows[0].animation_title,
            "Animation à compléter — ligne 2",
        )

    def test_a_file_still_needs_one_recognized_column(self):
        preview = preview_session_csv(self._upload("valeur\n", header="colonne_inconnue\n"))

        self.assertFalse(preview.is_valid)
        self.assertIn("Aucune colonne reconnue", preview.issues[0].message)

    def test_duplicate_or_empty_theme_tokens_are_rejected(self):
        duplicated = preview_session_csv(
            self._upload("Animation A;Salle;Sol|sol;Salle A;45;30;mercredi;10:00;;\n")
        )
        empty = preview_session_csv(
            self._upload("Animation B;Salle;Sol||Eau;Salle B;45;30;mercredi;11:00;;\n")
        )

        self.assertIn("dupliquée", duplicated.issues[0].message)
        self.assertIn("valeur vide", empty.issues[0].message)

    def test_same_animation_accepts_themes_in_a_different_order(self):
        preview = preview_session_csv(
            self._upload(
                "Animation A;Extérieur;Sol|Biodiversité;Pôle A;"
                "45;30;mercredi;10:00;;\n"
                "Animation A;Extérieur;Biodiversité|Sol;Pôle A;"
                "45;30;jeudi;10:00;;\n"
            )
        )

        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(len(preview.rows), 2)
        self.assertEqual(
            {row.theme_slugs for row in preview.rows},
            {("biodiversite", "sol")},
        )

    def test_existing_animation_taxonomy_is_updated_on_confirmation(self):
        preview = preview_session_csv(
            self._upload(
                "Le sol vivant;Extérieur;Sol|Biodiversité;Pôle sols;45;30;mercredi;10:00;;\n"
            )
        )

        self.assertTrue(preview.is_valid, preview.issues)
        import_session_payload([row.as_payload() for row in preview.rows])
        self.animation.refresh_from_db()

        self.assertEqual(
            self.animation.venue_category,
            Animation.VenueCategory.OUTDOOR,
        )
        self.assertEqual(
            set(self.animation.themes.values_list("slug", flat=True)),
            {"sol", "biodiversite"},
        )

    def test_existing_animation_change_after_preview_is_rejected(self):
        preview = preview_session_csv(
            self._upload("Le sol vivant;Extérieur;Sol;Pôle sols;45;30;mercredi;10:00;;\n")
        )
        self.assertTrue(preview.is_valid, preview.issues)
        self.animation.themes.add(Theme.objects.get(slug="eau"))

        with self.assertRaises(SessionImportError):
            import_session_payload([row.as_payload() for row in preview.rows])

        self.animation.refresh_from_db()
        self.assertEqual(
            self.animation.venue_category,
            Animation.VenueCategory.INDOOR,
        )
        self.assertEqual(Session.objects.count(), 0)

    def test_theme_change_after_preview_rolls_back_the_import(self):
        preview = preview_session_csv(
            self._upload("Nouvelle animation;Extérieur;Eau;Pôle eau;45;30;mercredi;10:00;;\n")
        )
        self.assertTrue(preview.is_valid, preview.issues)
        Theme.objects.filter(slug="eau").update(is_active=False)

        with self.assertRaises(SessionImportError):
            import_session_payload([row.as_payload() for row in preview.rows])

        self.assertFalse(Animation.objects.filter(title="Nouvelle animation").exists())
        self.assertEqual(Session.objects.count(), 0)

    def test_animation_slug_race_is_reported_without_breaking_the_transaction(self):
        preview = preview_session_csv(
            self._upload("Nouvelle animation;Extérieur;Eau;Pôle eau;45;30;mercredi;10:00;;\n")
        )
        self.assertTrue(preview.is_valid, preview.issues)

        with (
            patch(
                "operations.imports._available_animation_slug",
                return_value=self.animation.slug,
            ),
            patch.object(Animation, "full_clean", return_value=None),
            self.assertRaises(SessionImportError),
        ):
            import_session_payload([row.as_payload() for row in preview.rows])

        self.assertFalse(Animation.objects.filter(title="Nouvelle animation").exists())
        self.assertEqual(Session.objects.count(), 0)

    def test_every_invalid_source_line_is_reported_and_nothing_is_written(self):
        preview = preview_session_csv(
            self._upload(
                "Animation A;Salle;Sol;Salle A;0;30;mercredi;10:00;;\n"
                "Animation B;Salle;Sol;Salle B;45;30;vendredi;11:00;;\n"
            )
        )

        self.assertFalse(preview.is_valid)
        self.assertEqual([issue.line for issue in preview.issues], [2, 3])
        self.assertEqual(Animation.objects.count(), 1)
        self.assertEqual(Session.objects.count(), 0)

    def test_duplicate_times_are_rejected_but_overlapping_times_are_allowed(self):
        duplicate = preview_session_csv(
            self._upload("Animation A;Salle;Sol;Salle A;45;30;mercredi;10:00,10:00;;\n")
        )
        overlapping = preview_session_csv(
            self._upload("Animation A;Salle;Sol;Salle A;45;30;mercredi;10:00,10:30;;\n")
        )

        self.assertFalse(duplicate.is_valid)
        self.assertIn("dupliqué", duplicate.issues[0].message)
        self.assertTrue(overlapping.is_valid, overlapping.issues)
        self.assertEqual(
            [(row.starts_at.isoformat(), row.ends_at.isoformat()) for row in overlapping.rows],
            [("10:00:00", "10:45:00"), ("10:30:00", "11:15:00")],
        )

        sessions = import_session_payload([row.as_payload() for row in overlapping.rows])

        self.assertEqual(len(sessions), 2)
        self.assertEqual(Session.objects.count(), 2)

    def test_invalid_responsible_email_is_rejected(self):
        preview = preview_session_csv(
            self._upload(
                "Animation A;Salle;Sol;Salle A;45;30;mercredi;10:00;Alice;adresse-invalide\n"
            )
        )

        self.assertFalse(preview.is_valid)
        self.assertIn("courriel", preview.issues[0].message)

    def test_unquoted_times_in_comma_delimited_csv_are_not_silently_lost(self):
        header = (
            "titre_animation,categorie,thematiques,lieu_de_rendez_vous,"
            "duree,jauge,jour,horaires,"
            "responsable,email_responsable\n"
        )
        preview = preview_session_csv(
            self._upload(
                "Le sol vivant,Salle,Sol,Pôle sols,45,30,mercredi,09:00,10:00,,\n",
                header=header,
            )
        )

        self.assertFalse(preview.is_valid)
        self.assertIn("guillemets", preview.issues[0].message)
        self.assertEqual(preview.rows, [])

    def test_optional_contact_columns_may_be_omitted_in_quoted_comma_csv(self):
        header = (
            "titre_animation,categorie,thematiques,lieu_de_rendez_vous,duree,jauge,jour,horaires\n"
        )
        preview = preview_session_csv(
            self._upload(
                'Le sol vivant,Salle,Sol,Pôle sols,45,30,mercredi,"09:00,10:00"\n',
                header=header,
            )
        )

        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(len(preview.rows), 2)
        self.assertEqual({row.organizer for row in preview.rows}, {""})
        self.assertEqual({row.organizer_email for row in preview.rows}, {""})

    def test_same_new_animation_cannot_have_two_durations(self):
        preview = preview_session_csv(
            self._upload(
                "Animation A;Salle;Sol;Salle A;45;30;mercredi;10:00;;\n"
                "Animation A;Salle;Sol;Salle A;60;30;jeudi;10:00;;\n"
            )
        )

        self.assertFalse(preview.is_valid)
        self.assertEqual(len(preview.rows), 1)
        self.assertIn("informations différentes", preview.issues[0].message)

    def test_confirmation_is_atomic_when_database_state_has_changed(self):
        preview = preview_session_csv(
            self._upload(
                "Nouvelle animation;Extérieur;Biodiversité;Salle neuve;"
                "45;30;mercredi;10:00;;\n"
                "Le sol vivant;Extérieur;Biodiversité;Pôle sols;"
                "45;30;mercredi;11:00;;\n"
            )
        )
        self.assertTrue(preview.is_valid, preview.issues)
        Session.objects.create(
            animation=self.animation,
            date=preview.rows[1].date,
            starts_at=preview.rows[1].starts_at,
            ends_at=preview.rows[1].ends_at,
            location=preview.rows[1].location,
            max_capacity=30,
        )

        with self.assertRaises(SessionImportError):
            import_session_payload([row.as_payload() for row in preview.rows])

        self.assertEqual(Session.objects.count(), 1)
        self.assertFalse(Animation.objects.filter(title="Nouvelle animation").exists())
        self.animation.refresh_from_db()
        self.assertEqual(
            self.animation.venue_category,
            Animation.VenueCategory.INDOOR,
        )
        self.assertEqual(
            set(self.animation.themes.values_list("slug", flat=True)),
            {"sol"},
        )

    def test_tampered_preview_duration_is_rejected(self):
        preview = preview_session_csv(
            self._upload("Le sol vivant;Salle;Sol;Pôle sols;45;30;mercredi;10:00;;\n")
        )
        payload = preview.rows[0].as_payload()
        payload["duration_minutes"] = 60

        with self.assertRaises(SessionImportError):
            import_session_payload([payload])

        self.assertEqual(Session.objects.count(), 0)

    def test_staff_can_preview_then_confirm_from_the_import_view(self):
        staff = get_user_model().objects.create_user(
            username="importer", password="secret", is_staff=True
        )
        staff.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="catalogue",
                codename__in=("add_animation", "change_animation", "add_session"),
            )
        )
        self.client.force_login(staff)

        response = self.client.post(
            reverse("operations:session-import"),
            {
                "file": self._upload(
                    "Le sol vivant;Salle;Sol|Biodiversité;Pôle sols;45;30;jeudi;14:00;LTNM;\n"
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aperçu")
        self.assertContains(
            response,
            '<th scope="col">Catégorie</th>',
            html=True,
        )
        self.assertContains(
            response,
            '<th scope="col">Thématiques</th>',
            html=True,
        )
        self.assertContains(response, "Salle")
        self.assertContains(response, "Biodiversité, Sol")
        self.assertEqual(Session.objects.count(), 0)

        response = self.client.post(reverse("operations:session-import"), {"action": "confirm"})
        self.assertRedirects(response, reverse("operations:session-import"))
        self.assertEqual(Session.objects.count(), 1)
        session = Session.objects.get()
        self.animation.refresh_from_db()
        self.assertEqual(
            set(self.animation.themes.values_list("slug", flat=True)),
            {"sol", "biodiversite"},
        )
        self.assertTrue(
            LogEntry.objects.filter(
                user=staff,
                action_flag=ADDITION,
                object_id=str(session.pk),
            ).exists()
        )
        self.assertTrue(
            LogEntry.objects.filter(
                user=staff,
                action_flag=CHANGE,
                content_type__app_label="catalogue",
                content_type__model="animation",
                object_id=str(self.animation.pk),
            ).exists()
        )

    def test_import_page_links_to_a_downloadable_csv_template(self):
        staff = get_user_model().objects.create_user(
            username="template-viewer", password="secret", is_staff=True
        )
        staff.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="catalogue",
                codename__in=("add_animation", "change_animation", "add_session"),
            )
        )
        self.client.force_login(staff)
        Theme.objects.filter(slug="eau").update(is_active=False)

        response = self.client.get(reverse("operations:session-import"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Importer des séances")
        self.assertContains(response, "Catégorie et thématiques à importer")
        self.assertContains(response, "<code>Salle</code>", html=True)
        self.assertContains(response, "<code>Extérieur</code>", html=True)
        self.assertContains(response, "<code>Sol</code>", html=True)
        self.assertContains(response, "<code>Biodiversité</code>", html=True)
        self.assertContains(response, "Fichier CSV des séances")
        self.assertNotIn(
            "eau",
            set(response.context["active_themes"].values_list("slug", flat=True)),
        )
        self.assertContains(response, reverse("operations:session-import-template"))
        self.assertContains(response, "Télécharger le modèle CSV")
        self.assertContains(response, "Aucune colonne n’est obligatoire")
        self.assertContains(response, "60 min")

    def test_csv_template_is_utf8_bom_and_can_be_imported(self):
        staff = get_user_model().objects.create_user(
            username="template-downloader", password="secret", is_staff=True
        )
        staff.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="catalogue",
                codename__in=("add_animation", "change_animation", "add_session"),
            )
        )
        self.client.force_login(staff)

        response = self.client.get(reverse("operations:session-import-template"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="modele_import_animations.csv"',
        )
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        rows = list(
            csv.reader(
                StringIO(response.content.decode("utf-8-sig")),
                delimiter=";",
            )
        )
        self.assertEqual(
            rows[0],
            [
                "titre_animation",
                "categorie",
                "thematiques",
                "lieu_de_rendez_vous",
                "duree",
                "jauge",
                "jour",
                "horaires",
                "responsable",
                "email_responsable",
            ],
        )
        self.assertEqual(len(rows), 3)

        preview = preview_session_csv(
            SimpleUploadedFile(
                "modele_import_animations.csv",
                response.content,
                content_type="text/csv",
            )
        )
        self.assertTrue(preview.is_valid, preview.issues)
        self.assertEqual(len(preview.rows), 5)

    def test_csv_template_has_the_same_permissions_as_import(self):
        response = self.client.get(reverse("operations:session-import-template"))
        self.assertEqual(response.status_code, 302)

        staff = get_user_model().objects.create_user(
            username="partial-template-user", password="secret", is_staff=True
        )
        staff.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="catalogue",
                codename__in=("add_animation", "add_session"),
            )
        )
        self.client.force_login(staff)

        response = self.client.get(reverse("operations:session-import-template"))
        self.assertEqual(response.status_code, 403)
