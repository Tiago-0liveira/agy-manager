import unittest

from agym.integration.errors import IntegrationError
from agym.integration.protocol import failure, success, validate_protocol


class ProtocolTests(unittest.TestCase):
    def test_envelopes_and_version(self):
        self.assertEqual(success({"x": 1})["protocol"], {"major": 1, "minor": 0})
        self.assertFalse(failure("RUN_NOT_FOUND", "Run not found")["ok"])
        with self.assertRaises(IntegrationError):
            validate_protocol(2)
