from unittest.mock import patch

from django.test import TestCase


class HealthEndpointTests(TestCase):
    def test_liveness_is_public_and_does_not_expose_details(self):
        response = self.client.get("/healthz/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(
            response["Cache-Control"],
            "max-age=0, no-cache, no-store, must-revalidate, private",
        )

    def test_liveness_rejects_post(self):
        self.assertEqual(self.client.post("/healthz/").status_code, 405)

    def test_readiness_checks_database_and_cache(self):
        response = self.client.get("/readyz/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch("config.health.connection.cursor", side_effect=OSError("database unavailable"))
    def test_readiness_returns_generic_503_when_dependency_fails(self, _cursor):
        response = self.client.get("/readyz/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
