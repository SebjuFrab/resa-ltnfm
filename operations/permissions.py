"""Permission bundles for the internal FRAB workflow."""

REGISTRATION_DETAIL_PERMISSIONS = (
    "inscriptions.view_institution",
    "inscriptions.view_teacher",
    "inscriptions.view_registration",
    "inscriptions.view_reservation",
    "communication.view_emaillog",
)

REGISTRATION_MANAGE_PERMISSIONS = (
    *REGISTRATION_DETAIL_PERMISSIONS,
    "catalogue.view_session",
    "inscriptions.add_institution",
    "inscriptions.add_teacher",
    "inscriptions.change_teacher",
    "inscriptions.add_registration",
    "inscriptions.change_registration",
    "inscriptions.add_reservation",
    "inscriptions.change_reservation",
)
