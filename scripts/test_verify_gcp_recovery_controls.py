"""CLI contracts for explicit, read-only control verification and ledger gating."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import verify_gcp_recovery_controls as command
from gcp_token_identity import VerifiedGoogleUser
from test_run_contract import intent


class _TokenSource:
    def access_token(self):
        return "explicit-test-token"


class _Snapshot:
    def __init__(self, run_id, manifest, generation, lease=None):
        self.run_id = run_id
        self.manifest = deepcopy(manifest)
        self.generation = generation
        self.metadata = {"lease": {"owner": lease, "expires_at": "later" if lease else None}}


class _Ledger:
    def __init__(self, manifest):
        self.snapshot = _Snapshot(manifest["run_id"], manifest, 5)

    def read(self, run_id):
        return self.snapshot

    def acquire_lease(self, run_id, *, expected_generation, owner, ttl):
        assert expected_generation == self.snapshot.generation
        self.snapshot = _Snapshot(run_id, self.snapshot.manifest, expected_generation + 1, owner)
        return self.snapshot

    def record_recovery_control_verification(self, run_id, *, expected_generation, lease_owner, observations):
        assert expected_generation == self.snapshot.generation
        self.snapshot.manifest["state"] = "prepared"
        for item in self.snapshot.manifest["controls"]:
            item["status"] = "present"
        self.snapshot.manifest["recovery_control_receipts"] = [{"receipt_id": "f" * 64}]
        self.snapshot.generation += 1
        return self.snapshot

    def release_lease(self, run_id, *, expected_generation, owner):
        assert expected_generation == self.snapshot.generation
        self.snapshot.generation += 1
        self.snapshot.metadata["lease"]["owner"] = None
        self.snapshot.metadata["lease"]["expires_at"] = None
        return self.snapshot


class VerifyRecoveryControlsCliTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.manifest = intent()
        self.manifest["created_at"] = now.isoformat().replace("+00:00", "Z")
        self.manifest["deadline_at"] = (now + timedelta(minutes=50)).isoformat().replace("+00:00", "Z")
        self.manifest["finance"]["credit_observed_at"] = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        self.manifest["finance"]["credit_expires_at"] = (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        self.temp = tempfile.TemporaryDirectory()
        self.intent_path = Path(self.temp.name) / "intent.json"
        self.intent_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def args(self, *, ledger_bucket="portfolio-ledger-private"):
        return [
            "--intent", str(self.intent_path),
            "--credentials-file", str(Path(self.temp.name) / "operator.json"),
            "--ledger-bucket", ledger_bucket,
            "--ledger-project-id", "recovery-lab-456",
            "--expected-email", "operator@example.com",
            "--expected-auditor-id", "google-user-12345678901234567890",
        ]

    def test_successful_verification_prepares_only_the_exact_run(self):
        ledger = _Ledger(self.manifest)
        class FakeVerifier:
            def __init__(self, *args, **kwargs):
                pass
            def verify(self):
                return [{"type": control["type"], "id": control["id"], "response_sha256": "a" * 64}
                        for control in self_outer.manifest["controls"]]
        self_outer = self
        stdout = StringIO()
        with (
            patch.object(command, "_build_token_source", return_value=(_TokenSource(), object())),
            patch.object(command, "verify_access_token_principal", return_value=VerifiedGoogleUser(
                auditor_id="google-user-12345678901234567890", email="operator@example.com"
            )),
            patch.object(command, "GCPRecoveryControlVerifier", FakeVerifier),
            patch.object(command, "_build_ledger", return_value=ledger),
            redirect_stdout(stdout),
        ):
            result = command.main(self.args())
        self.assertEqual(result, 0)
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(receipt["state"], "prepared")
        self.assertEqual(receipt["verified_controls"], 4)
        self.assertEqual(receipt["receipt_id"], "f" * 64)
        self.assertIsNone(ledger.snapshot.metadata["lease"]["owner"])

    def test_bucket_mismatch_fails_before_loading_credentials_or_constructing_cloud_clients(self):
        stderr = StringIO()
        with (
            patch.object(command, "_build_token_source") as token_source,
            patch.object(command, "_build_ledger") as build_ledger,
            redirect_stderr(stderr),
        ):
            result = command.main(self.args(ledger_bucket="different-ledger-private"))
        self.assertEqual(result, 2)
        token_source.assert_not_called()
        build_ledger.assert_not_called()
        self.assertIn("ledger bucket must match", stderr.getvalue())

    def test_wrong_google_user_subject_fails_before_ledger_or_control_calls(self):
        stderr = StringIO()
        with (
            patch.object(command, "_build_token_source", return_value=(_TokenSource(), object())),
            patch.object(command, "verify_access_token_principal", return_value=VerifiedGoogleUser(
                auditor_id="google-user-99999999999999999999", email="operator@example.com"
            )),
            patch.object(command, "_build_ledger") as build_ledger,
            redirect_stderr(stderr),
        ):
            result = command.main(self.args())
        self.assertEqual(result, 2)
        build_ledger.assert_not_called()
        self.assertIn("verification rejected", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
