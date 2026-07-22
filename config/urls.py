from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from inscriptions.security import rate_limit

admin_login = rate_limit(
    "admin-login", attempts=10, window_seconds=900
)(admin.site.login)

urlpatterns = [
    path("admin/login/", admin_login, name="admin-login-limited"),
    path("admin/", admin.site.urls),
    path(
        "",
        RedirectView.as_view(pattern_name="operations:dashboard", permanent=False),
        name="home",
    ),
    path("operations/", include("operations.urls")),
]

if settings.ENABLE_LEGACY_PUBLIC_FLOW:
    urlpatterns.append(path("ancien-parcours/", include("inscriptions.urls")))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
