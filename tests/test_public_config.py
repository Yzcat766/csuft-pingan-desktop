"""Public distributions must never fall back to an embedded service credential."""
import unittest
from unittest.mock import Mock, patch

from api import ApiError, SchoolClient, redact


class PublicConfigurationTests(unittest.TestCase):
    def test_missing_service_authorization_stops_before_network(self):
        session = Mock()
        with patch("api.AUTH", ""):
            with self.assertRaisesRegex(ApiError, "PINGAN_API_AUTHORIZATION"):
                SchoolClient("mock-openid", session).login()
        session.request.assert_not_called()

    def test_supplied_service_authorization_is_forwarded(self):
        session = Mock()
        session.request.return_value.json.return_value = {"access_token": "mock-token"}
        with patch("api.AUTH", "Basic bW9jazptb2Nr"):
            SchoolClient("mock-openid", session).login()
        self.assertEqual(session.request.call_args.kwargs["headers"]["Authorization"], "Basic bW9jazptb2Nr")

    def test_service_credential_is_redacted(self):
        with patch("api.AUTH", "custom-test-credential"):
            self.assertNotIn("custom-test-credential", redact("echo custom-test-credential"))
