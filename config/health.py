import secrets

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def healthz(request):
    """Liveness probe: the Django process can answer HTTP requests."""
    return JsonResponse({"status": "ok"})


@never_cache
@require_GET
def readyz(request):
    """Readiness probe: PostgreSQL and the shared cache are available."""
    cache_key = f"health:{secrets.token_hex(8)}"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise RuntimeError("unexpected database health response")
        cache.set(cache_key, "ok", timeout=10)
        if cache.get(cache_key) != "ok":
            raise RuntimeError("unexpected cache health response")
        cache.delete(cache_key)
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
