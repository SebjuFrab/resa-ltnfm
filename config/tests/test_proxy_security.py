from django.test import SimpleTestCase, override_settings


@override_settings(
    ALLOWED_HOSTS=["resa-ltnfm.agrobio-bretagne.org"],
    SECURE_SSL_REDIRECT=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    SECURE_HSTS_SECONDS=31_536_000,
    SECURE_HSTS_INCLUDE_SUBDOMAINS=True,
    SECURE_HSTS_PRELOAD=True,
    USE_X_FORWARDED_HOST=True,
)
class ProxySecurityTests(SimpleTestCase):
    domain = "resa-ltnfm.agrobio-bretagne.org"

    def test_forwarded_https_request_is_accepted_without_redirect_loop(self):
        response = self.client.get(
            "/healthz/",
            HTTP_HOST=self.domain,
            HTTP_X_FORWARDED_HOST=self.domain,
            HTTP_X_FORWARDED_PROTO="https",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("max-age=31536000", response["Strict-Transport-Security"])

    def test_plain_http_request_is_redirected_to_https(self):
        response = self.client.get("/healthz/", HTTP_HOST=self.domain)

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], f"https://{self.domain}/healthz/")

    def test_unknown_host_is_rejected(self):
        response = self.client.get(
            "/healthz/",
            HTTP_HOST="attacker.example",
            HTTP_X_FORWARDED_PROTO="https",
        )

        self.assertEqual(response.status_code, 400)
