from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from inscriptions.security import rate_limit


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class RateLimitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def test_blocks_after_limit(self):
        @rate_limit("test", attempts=2, window_seconds=60)
        def view(request):
            return HttpResponse("ok")

        request = self.factory.post("/", REMOTE_ADDR="192.0.2.1")
        self.assertEqual(view(request).status_code, 200)
        self.assertEqual(view(request).status_code, 200)
        self.assertEqual(view(request).status_code, 429)

    def test_does_not_limit_get_by_default(self):
        @rate_limit("test-get", attempts=1, window_seconds=60)
        def view(request):
            return HttpResponse("ok")

        request = self.factory.get("/", REMOTE_ADDR="192.0.2.1")
        self.assertEqual(view(request).status_code, 200)
        self.assertEqual(view(request).status_code, 200)

