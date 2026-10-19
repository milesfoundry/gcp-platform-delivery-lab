"""Tests for the explicit, confirmation-bound project launcher CLI."""

from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import patch

from launch_gcp_run import _parser, main


class LaunchGCPRunTests(unittest.TestCase):
    def test_retry_requires_the_explicit_flag(self):
        arguments = [
            "--run-id", "lab-run-12345",
            "--credentials-file", "/tmp/operator.json",
            "--ledger-bucket", "portfolio-ledger-private",
            "--ledger-project-id", "recovery-lab-456",
            "--expected-email", "operator@example.com",
            "--expected-google-user", "google-user-1234567890",
            "--confirm-project-id", "sample-lab-123",
            "--confirm-intent-sha256", "a" * 64,
        ]
        self.assertFalse(_parser().parse_args(arguments).retry_failed)
        self.assertTrue(
            _parser().parse_args(arguments + ["--retry-failed"]).retry_failed
        )

    def test_invalid_run_id_fails_before_credentials_or_network_are_constructed(self):
        arguments = [
            "--run-id", "../unbounded",
            "--credentials-file", "/tmp/operator.json",
            "--ledger-bucket", "portfolio-ledger-private",
            "--ledger-project-id", "recovery-lab-456",
            "--expected-email", "operator@example.com",
            "--expected-google-user", "google-user-1234567890",
            "--confirm-project-id", "sample-lab-123",
            "--confirm-intent-sha256", "a" * 64,
        ]
        stderr = io.StringIO()
        with patch("launch_gcp_run._load_token_source") as load_credentials:
            with redirect_stderr(stderr), self.assertRaises(SystemExit):
                main(arguments)
        load_credentials.assert_not_called()
        self.assertIn("bounded lab-prefixed identifier", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
