from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = False
_forbidden_secrets = {
    "unsafe-development-key",
    "change-me",
    "change-me-in-production",
}
_secret_key = SECRET_KEY  # noqa: F405
if (
    _secret_key in _forbidden_secrets
    or _secret_key.startswith("django-insecure-")
    or len(_secret_key) < 50
    or len(set(_secret_key)) < 10
):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY doit être aléatoire et contenir au moins 50 caractères."
    )
if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":  # noqa: F405
    raise ImproperlyConfigured("PostgreSQL est obligatoire en production.")
for _database_variable in (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_HOST",
):
    if not env(_database_variable):  # noqa: F405
        raise ImproperlyConfigured(f"{_database_variable} doit être défini en production.")
if env("POSTGRES_PASSWORD") in {"change-me", "password", "postgres"}:  # noqa: F405
    raise ImproperlyConfigured("POSTGRES_PASSWORD utilise une valeur interdite.")
if not env("REDIS_URL"):  # noqa: F405
    raise ImproperlyConfigured("REDIS_URL doit être défini en production.")
_unsafe_email_backends = {
    "django.core.mail.backends.console.EmailBackend",
    "django.core.mail.backends.locmem.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend",
    "django.core.mail.backends.filebased.EmailBackend",
}
if EMAIL_BACKEND in _unsafe_email_backends:  # noqa: F405
    raise ImproperlyConfigured("Un backend SMTP doit être configuré en production.")
if EMAIL_USE_TLS == EMAIL_USE_SSL:  # noqa: F405
    raise ImproperlyConfigured(
        "Activez exactement un transport SMTP chiffré : EMAIL_USE_TLS ou EMAIL_USE_SSL."
    )

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)  # noqa: F405
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
if env_bool("TRUST_PROXY_HEADERS", False):  # noqa: F405
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
