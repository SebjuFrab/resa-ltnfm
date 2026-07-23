import hashlib
import ipaddress
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse


def _client_address(request):
    """Return a pseudonymous client key, trusting forwarding headers only by opt-in."""
    address = request.META.get("REMOTE_ADDR", "unknown")
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").strip()
        # The edge proxy must replace X-Forwarded-For with one address. A chain is
        # rejected instead of trusting a client-controlled first value.
        if forwarded and "," not in forwarded:
            try:
                address = str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
    return hashlib.sha256(address.encode("utf-8")).hexdigest()


def rate_limit(scope, *, attempts, window_seconds, methods=("POST",)):
    """Small cache-backed fixed-window limiter for public form endpoints."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                return view(request, *args, **kwargs)

            cache_key = f"rate:{scope}:{_client_address(request)}"
            try:
                if cache.add(cache_key, 1, timeout=window_seconds):
                    count = 1
                else:
                    try:
                        count = cache.incr(cache_key)
                    except ValueError:
                        cache.set(cache_key, 1, timeout=window_seconds)
                        count = 1
            except Exception:
                return HttpResponse(
                    "Le service est temporairement indisponible. Réessayez plus tard.",
                    status=503,
                    content_type="text/plain; charset=utf-8",
                )

            if count > attempts:
                response = HttpResponse(
                    "Trop de tentatives. Veuillez réessayer dans quelques minutes.",
                    status=429,
                    content_type="text/plain; charset=utf-8",
                )
                response["Retry-After"] = str(window_seconds)
                return response
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
