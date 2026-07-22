from django.conf import settings


def event_context(request):
    return {
        "event_dates": settings.EVENT_DATES,
        "organization_email": settings.ORGANIZATION_EMAIL,
        "organization_phone": settings.ORGANIZATION_PHONE,
    }

