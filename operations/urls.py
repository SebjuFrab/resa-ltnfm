from django.urls import path

from . import views

app_name = "operations"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("animations/", views.animation_list, name="animation-list"),
    path("groupes/nouveau/", views.registration_create, name="registration-create"),
    path(
        "groupes/code/aleatoire/",
        views.group_code_suggestion,
        name="group-code-suggestion",
    ),
    path(
        "groupes/<uuid:reference>/",
        views.registration_detail,
        name="registration-detail",
    ),
    path(
        "groupes/<uuid:reference>/animations/",
        views.registration_planning,
        name="registration-planning",
    ),
    path(
        "groupes/<uuid:reference>/verifier/",
        views.registration_review,
        name="registration-review",
    ),
    path(
        "groupes/<uuid:reference>/modifier/",
        views.registration_update,
        name="registration-update",
    ),
    path(
        "groupes/<uuid:reference>/annuler/",
        views.registration_cancel,
        name="registration-cancel",
    ),
    path(
        "groupes/<uuid:reference>/renvoyer/",
        views.registration_resend,
        name="registration-resend",
    ),
    path("publipostage/", views.mailing_create, name="mailing-create"),
    path(
        "publipostage/<int:campaign_id>/",
        views.mailing_detail,
        name="mailing-detail",
    ),
    path("import/seances/", views.session_import, name="session-import"),
    path(
        "import/seances/modele.csv",
        views.session_import_template,
        name="session-import-template",
    ),
    path("import/groupes/", views.group_import, name="group-import"),
    path(
        "import/groupes/modele.csv",
        views.group_import_template,
        name="group-import-template",
    ),
    path("exports/telecharger/", views.export_download, name="export-download"),
    path("exports/inscriptions.csv", views.export_registrations, name="export-registrations"),
    path("exports/reservations.csv", views.export_reservations, name="export-reservations"),
    path("exports/seances.csv", views.export_sessions, name="export-sessions"),
]
