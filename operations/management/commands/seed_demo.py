import uuid
from datetime import date, time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from catalogue.models import Animation, Category, SchoolLevel, Session
from inscriptions.models import (
    GroupFamily,
    Institution,
    Registration,
    RegistrationEvent,
    Reservation,
    Teacher,
)
from inscriptions.services.tokens import token_digest


class Command(BaseCommand):
    help = "Crée un petit jeu de données de démonstration idempotent."

    @transaction.atomic
    def handle(self, *args, **options):
        agriculture, _ = Category.objects.get_or_create(
            slug="agriculture-durable",
            defaults={"name": "Agriculture durable"},
        )
        biodiversity, _ = Category.objects.get_or_create(
            slug="biodiversite",
            defaults={"name": "Biodiversité"},
        )
        second, _ = SchoolLevel.objects.get_or_create(
            code="LYC_2DE",
            defaults={"label": "Seconde", "sort_order": 30},
        )
        first, _ = SchoolLevel.objects.get_or_create(
            code="LYC_1ERE",
            defaults={"label": "Première", "sort_order": 40},
        )
        school_family, _ = GroupFamily.objects.get_or_create(
            slug="lycee-agricole",
            defaults={"name": "Lycée agricole", "sort_order": 10},
        )

        soil, _ = Animation.objects.get_or_create(
            slug="vie-du-sol",
            defaults={
                "title": "La vie secrète du sol",
                "short_description": "Observer la biodiversité qui rend les sols fertiles.",
                "description": "Atelier d'observation et d'échanges autour de la vie du sol.",
                "category": biodiversity,
                "indicative_duration": 45,
                "instructions": "Prévoir des chaussures adaptées à une activité extérieure.",
                "accessibility": "Accessible aux personnes à mobilité réduite.",
            },
        )
        soil.recommended_levels.add(second, first)
        climate, _ = Animation.objects.get_or_create(
            slug="agriculture-et-climat",
            defaults={
                "title": "Agriculture et climat",
                "short_description": "Comprendre les leviers agricoles face au climat.",
                "description": "Conférence participative avec des professionnels du territoire.",
                "category": agriculture,
                "indicative_duration": 60,
                "instructions": "Se présenter dix minutes avant le début.",
                "accessibility": "Salle accessible.",
            },
        )
        climate.recommended_levels.add(second, first)

        session_definitions = (
            (soil, date(2026, 9, 23), time(10, 0), time(10, 45), "Pôle sols", 30),
            (climate, date(2026, 9, 23), time(11, 0), time(12, 0), "Salle A", 80),
            (soil, date(2026, 9, 24), time(10, 0), time(10, 45), "Pôle sols", 30),
            (climate, date(2026, 9, 24), time(14, 0), time(15, 0), "Salle A", 80),
        )
        sessions = []
        for animation, day, starts_at, ends_at, location, capacity in session_definitions:
            session, _ = Session.objects.get_or_create(
                animation=animation,
                date=day,
                starts_at=starts_at,
                ends_at=ends_at,
                location=location,
                defaults={
                    "max_capacity": capacity,
                    "status": Session.Status.OPEN,
                    "organizer": "Équipe LTNM",
                    "organizer_email": "animations@example.test",
                },
            )
            sessions.append(session)

        institution, _ = Institution.objects.get_or_create(
            name="Lycée agricole de démonstration",
            postal_code="35000",
            defaults={
                "institution_type": Institution.Type.AGRICULTURAL,
                "address": "1 rue des Champs",
                "city": "Rennes",
                "department": "35",
                "phone": "02 99 00 00 00",
                "administrative_email": "administration@example.test",
            },
        )
        teacher, _ = Teacher.objects.get_or_create(
            institution=institution,
            email="marie.dupont@example.test",
            defaults={
                "first_name": "Marie",
                "last_name": "Dupont",
                "phone": "06 00 00 00 00",
            },
        )
        now = timezone.now()
        registration, created = Registration.objects.get_or_create(
            reference=uuid.UUID("f6fb9c64-ef1f-4b6a-a3d9-d59483fd14a4"),
            defaults={
                "institution": institution,
                "teacher": teacher,
                "group_name": "Seconde A",
                "family": school_family,
                "school_level": second,
                "student_count": 24,
                "chaperone_count": 2,
                "visit_date": date(2026, 9, 23),
                "status": Registration.Status.CONFIRMED,
                "edit_token_digest": token_digest("jeton-demo-non-communique"),
                "token_created_at": now,
                "confirmed_at": now,
            },
        )
        if created:
            RegistrationEvent.objects.create(
                registration=registration,
                event_type=RegistrationEvent.Type.CREATED,
                actor_kind=RegistrationEvent.ActorKind.SYSTEM,
                changes={"source": "seed_demo"},
            )
            RegistrationEvent.objects.create(
                registration=registration,
                event_type=RegistrationEvent.Type.CONFIRMED,
                actor_kind=RegistrationEvent.ActorKind.SYSTEM,
                changes={"source": "seed_demo"},
            )
        for session in sessions[:2]:
            Reservation.objects.get_or_create(
                registration=registration,
                session=session,
                status=Reservation.Status.ACTIVE,
                defaults={"student_count": 24, "chaperone_count": 2},
            )

        self.stdout.write(self.style.SUCCESS("Données de démonstration disponibles."))
