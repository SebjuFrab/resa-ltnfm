from django.urls import path

from . import views

urlpatterns = [
    path("", views.registration_start, name="registration-start"),
    path(
        "inscription/<uuid:reference>/planning/",
        views.draft_planning,
        name="registration-planning",
    ),
    path(
        "inscription/<uuid:reference>/verification/",
        views.draft_review,
        name="registration-review",
    ),
    path(
        "inscription/<uuid:reference>/confirmation/",
        views.registration_complete,
        name="registration-complete",
    ),
    path(
        "modifier/<uuid:reference>/",
        views.edit_link_entry,
        name="registration-edit-link",
    ),
    path("gestion/", views.manage_registration, name="registration-manage"),
    path(
        "gestion/informations/",
        views.manage_details,
        name="registration-manage-details",
    ),
    path(
        "gestion/planning/",
        views.manage_planning,
        name="registration-manage-planning",
    ),
    path(
        "gestion/annuler/",
        views.manage_cancellation,
        name="registration-manage-cancel",
    ),
]
