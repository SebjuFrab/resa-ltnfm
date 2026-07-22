from .permissions import REGISTRATION_MANAGE_PERMISSIONS


def staff_capabilities(request):
    user = request.user
    can_manage_registrations = (
        user.is_authenticated
        and user.is_staff
        and user.has_perms(REGISTRATION_MANAGE_PERMISSIONS)
    )
    return {"can_manage_registrations": can_manage_registrations}
