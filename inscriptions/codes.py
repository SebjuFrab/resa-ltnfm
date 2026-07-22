"""Génération des codes publics, lisibles et non séquentiels des groupes."""

from __future__ import annotations

import secrets

from django.apps import apps
from django.utils.text import slugify

FOOD_NAMES = (
    "abricot",
    "amande",
    "aneth",
    "artichaut",
    "basilic",
    "bergamote",
    "betterave",
    "bleuet",
    "cacao",
    "cannelle",
    "carotte",
    "cerise",
    "chataigne",
    "citron",
    "clementine",
    "coriandre",
    "courgette",
    "epinard",
    "fenouil",
    "figue",
    "fraise",
    "framboise",
    "gingembre",
    "grenade",
    "groseille",
    "haricot",
    "kiwi",
    "lavande",
    "lentille",
    "mandarine",
    "marron",
    "melon",
    "menthe",
    "mirabelle",
    "moutarde",
    "myrtille",
    "navet",
    "noisette",
    "noix",
    "olive",
    "orange",
    "paprika",
    "pasteque",
    "peche",
    "persil",
    "piment",
    "pistache",
    "poire",
    "poireau",
    "pomme",
    "potiron",
    "prune",
    "radis",
    "romarin",
    "safran",
    "sauge",
    "thym",
    "tomate",
    "truffe",
    "vanille",
)

QUALIFIERS = (
    "ambre",
    "argente",
    "boise",
    "brillant",
    "dore",
    "doux",
    "epice",
    "etoile",
    "fleuri",
    "fruite",
    "joyeux",
    "mielleux",
    "nacre",
    "parfume",
    "petillant",
    "sauvage",
    "solaire",
    "tendre",
    "vermeil",
    "vif",
)

MAX_GENERATION_ATTEMPTS = 200


def normalize_group_code(value: str) -> str:
    """Normalise une suggestion sous la forme utilisable dans les URLs et exports."""
    return slugify(value or "")[:80]


def generate_group_code_candidate() -> str:
    """Construit une combinaison alimentaire lisible, sans numéro séquentiel."""
    food = secrets.choice(FOOD_NAMES)
    qualifier = secrets.choice(QUALIFIERS)
    return normalize_group_code(f"{food}-{qualifier}")


def generate_unique_group_code(*, excluding_registration_id=None) -> str:
    """Renvoie une suggestion absente de la base au moment de la vérification.

    La contrainte ``UNIQUE`` du modèle reste l'autorité en cas de créations
    concurrentes. Le service d'inscription sait réessayer après une collision.
    """
    registration_model = apps.get_model("inscriptions", "Registration")
    registrations = registration_model.objects.all()
    if excluding_registration_id is not None:
        registrations = registrations.exclude(pk=excluding_registration_id)

    for _attempt in range(MAX_GENERATION_ATTEMPTS):
        candidate = generate_group_code_candidate()
        if not registrations.filter(group_code=candidate).exists():
            return candidate
    raise RuntimeError("Impossible de générer un code de groupe disponible.")
