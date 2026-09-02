from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from catalogue.models import SchoolLevel
from inscriptions.models import GroupFamily
from operations.group_imports import (
    GroupImportError,
    import_group_payload,
    preview_group_csv,
)
from operations.xlsx_group_import import (
    XlsxGroupImportError,
    prepare_group_workbook,
)


class Command(BaseCommand):
    help = (
        "Prévisualise ou importe le classeur XLSX des groupes. "
        "Sans --commit, aucune donnée n'est écrite."
    )

    def add_arguments(self, parser):
        parser.add_argument("workbook", help="Chemin du classeur .xlsx")
        parser.add_argument(
            "--actor",
            required=True,
            help="Nom d'utilisateur d'un membre de l'équipe traçant l'import",
        )
        parser.add_argument(
            "--group-sheet",
            default="import groupe 2",
            help="Nom de la feuille contenant les groupes",
        )
        parser.add_argument(
            "--level-sheet",
            default="niveau",
            help="Nom de la feuille contenant les niveaux disponibles en production",
        )
        parser.add_argument(
            "--keep-location",
            action="store_true",
            help="Conserver commune et département (ignorés par défaut)",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Effectuer l'import atomique après validation",
        )

    def handle(self, *args, **options):
        actor = (
            get_user_model()
            .objects.filter(username=options["actor"], is_staff=True, is_active=True)
            .first()
        )
        if actor is None:
            raise CommandError(
                "L'utilisateur --actor est introuvable, inactif ou n'appartient pas à l'équipe."
            )

        try:
            prepared = prepare_group_workbook(
                options["workbook"],
                group_sheet=options["group_sheet"],
                level_sheet=options["level_sheet"],
                keep_location=options["keep_location"],
            )
        except XlsxGroupImportError as error:
            raise CommandError(str(error)) from error

        required_level_codes = set(prepared.level_counts)
        active_level_codes = set(
            SchoolLevel.objects.filter(code__in=required_level_codes, is_active=True).values_list(
                "code", flat=True
            )
        )
        missing_level_codes = sorted(required_level_codes - active_level_codes)
        if missing_level_codes:
            raise CommandError(
                "Niveaux absents ou inactifs en production : "
                f"{', '.join(missing_level_codes)}. Ajoutez-les avant l'import."
            )

        required_families = {
            "lycee-agricole",
            "enseignement-superieur",
            "autre-public",
        }
        active_families = set(
            GroupFamily.objects.filter(slug__in=required_families, is_active=True).values_list(
                "slug", flat=True
            )
        )
        missing_families = sorted(required_families - active_families)
        if missing_families:
            raise CommandError(
                "Familles de groupes absentes ou inactives en production : "
                + ", ".join(missing_families)
            )

        upload = ContentFile(
            prepared.csv_content,
            name=f"{Path(options['workbook']).stem}.csv",
        )
        preview = preview_group_csv(upload)
        if not preview.is_valid:
            rendered_issues = []
            for issue in preview.issues:
                prefix = f"ligne {issue.line} : " if issue.line else ""
                rendered_issues.append(prefix + issue.message)
            raise CommandError(
                "Le classeur n'est pas importable :\n- " + "\n- ".join(rendered_issues)
            )

        self.stdout.write(f"Classeur validé : {prepared.group_count} groupes.")
        self.stdout.write("Répartition des niveaux :")
        for code, count in prepared.level_counts.items():
            self.stdout.write(f"  - {code} ({prepared.level_labels.get(code, code)}) : {count}")
        if prepared.realigned_row_count:
            self.stdout.write(
                f"Lignes décalées corrigées : {prepared.realigned_row_count} "
                f"({', '.join(map(str, prepared.realigned_lines))})."
            )
        if prepared.harmonized_contact_count:
            self.stdout.write(
                "Contacts dupliqués harmonisés avec leur première occurrence : "
                f"{prepared.harmonized_contact_count} "
                f"(lignes {', '.join(map(str, prepared.harmonized_contact_lines))})."
            )
        if not options["keep_location"]:
            self.stdout.write("Commune et département ignorés pour cet import.")

        if not options["commit"]:
            self.stdout.write(
                self.style.WARNING(
                    "SIMULATION UNIQUEMENT : aucune donnée écrite. "
                    "Relancez la même commande avec --commit pour importer."
                )
            )
            return

        try:
            registrations = import_group_payload(
                [row.as_payload() for row in preview.rows], actor_user=actor
            )
        except GroupImportError as error:
            rendered_issues = []
            for issue in error.issues:
                prefix = f"ligne {issue.line} : " if issue.line else ""
                rendered_issues.append(prefix + issue.message)
            raise CommandError(
                "L'import a été annulé sans écriture :\n- " + "\n- ".join(rendered_issues)
            ) from error

        self.stdout.write(
            self.style.SUCCESS(f"Import terminé : {len(registrations)} groupes créés.")
        )
