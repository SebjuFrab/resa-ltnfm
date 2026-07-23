from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from inscriptions.security import _client_address, rate_limit


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

    @override_settings(TRUST_PROXY_HEADERS=True)
    def test_uses_single_forwarded_address_from_trusted_proxy(self):
        request = self.factory.post(
            "/",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="192.0.2.20",
        )
        other = self.factory.post(
            "/",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="192.0.2.21",
        )

        self.assertNotEqual(_client_address(request), _client_address(other))

    @override_settings(TRUST_PROXY_HEADERS=True)
    def test_rejects_forwarded_chain_instead_of_trusting_first_value(self):
        forged = self.factory.post(
            "/",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="198.51.100.10, 192.0.2.20",
        )
        direct = self.factory.post("/", REMOTE_ADDR="127.0.0.1")

        self.assertEqual(_client_address(forged), _client_address(direct))
