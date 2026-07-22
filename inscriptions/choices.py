"""Shared choices tailored to the FRAB school recruitment area."""

DEPARTMENT_CHOICES = (
    (
        "Bretagne",
        (
            ("22", "22 — Côtes-d'Armor"),
            ("29", "29 — Finistère"),
            ("35", "35 — Ille-et-Vilaine"),
            ("56", "56 — Morbihan"),
        ),
    ),
    (
        "Départements limitrophes",
        (
            ("44", "44 — Loire-Atlantique"),
            ("49", "49 — Maine-et-Loire"),
            ("50", "50 — Manche"),
            ("53", "53 — Mayenne"),
        ),
    ),
)


def department_form_choices():
    return (("", "Sélectionner un département"), *DEPARTMENT_CHOICES)
