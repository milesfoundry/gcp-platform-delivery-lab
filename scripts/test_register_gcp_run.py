"""Tests for the explicit pre-provisioning run registration command."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import register_gcp_run
from run_ledger import GenerationConflict, LedgerNotFound, RunLedger
from test_run_contract import intent


@dataclass
class _Snapshot:
    run_id: str
    manifest: dict
    generation: int
    metadata: dict


class _FakeLedger:
    def __init__(self):
        self.created = []

    def create(self, value):
        self.created.append(value)
        return _Snapshot(value["run_id"], value, 7, {"intent_sha256": "a" * 64})


class _LossyObjectStore:
    def __init__(self):
        self.objects = {}
        self.create_calls = 0
        self.lose_first_response = True

    def create(self, name, content):
        self.create_calls += 1
        if name in self.objects:
            raise GenerationConflict(name)
        self.objects[name] = (content, 23)
        if self.lose_first_response:
            self.lose_first_response = False
            raise ConnectionError("simulated response loss")
        return 23

    def read(self, name):
        try:
            return self.objects[name]
        except KeyError as exc:
            raise LedgerNotFound(name) from exc

    def replace(self, name, content, *, generation):
        raise AssertionError("registration must not replace a manifest")


class RegisterGCPRunTests(unittest.TestCase):
    def setUp(self):
        self.document = intent()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.document["created_at"] = now.isoformat().replace("+00:00", "Z")
        self.document["deadline_at"] = (
            now + timedelta(minutes=50)
        ).isoformat().replace("+00:00", "Z")
        self.document["finance"]["credit_observed_at"] = (
            now - timedelta(minutes=1)
        ).isoformat().replace("+00:00", "Z")
        self.document["finance"]["credit_expires_at"] = (
            now + timedelta(days=30)
        ).isoformat().replace("+00:00", "Z")
        self.temporary = tempfile.TemporaryDirectory()
        self.intent_path = Path(self.temporary.name) / "intent.json"
        self.intent_path.write_text(json.dumps(self.document), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def _args(
        self,
        *,
        recovery_project_id="recovery-lab-456",
        ledger_bucket="portfolio-ledger-private",
    ):
        return [
            "--intent", str(self.intent_path),
            "--credentials-file", str(Path(self.temporary.name) / "human.json"),
            "--ledger-bucket", ledger_bucket,
            "--ledger-project-id", recovery_project_id,
            "--expected-email", "operator@example.com",
            "--expected-auditor-id", "google-user-12345678901234567890",
        ]

    def test_registers_the_exact_predeclared_run_and_prints_minimal_receipt(self):
        ledger = _FakeLedger()
        stdout = StringIO()
        with (
            patch("register_gcp_run._build_ledger", return_value=ledger) as build,
            redirect_stdout(stdout),
        ):
            result = register_gcp_run.main(self._args())

        self.assertEqual(result, 0)
        build.assert_called_once()
        self.assertEqual(ledger.created, [self.document])
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(receipt["run_id"], self.document["run_id"])
        self.assertEqual(receipt["state"], "control_bootstrapping")
        self.assertEqual(
            receipt["ledger_object"],
            f"runs/{self.document['run_id']}/manifest.json",
        )
        self.assertEqual(receipt["generation"], 7)
        self.assertEqual(receipt["intent_sha256"], "a" * 64)
        self.assertNotIn("credentials", receipt)

    def test_retry_after_lost_gcs_create_response_reuses_exact_manifest(self):
        store = _LossyObjectStore()
        ledger = RunLedger(store)
        first_stderr = StringIO()
        with (
            patch("register_gcp_run._build_ledger", return_value=ledger),
            redirect_stderr(first_stderr),
        ):
            first_result = register_gcp_run.main(self._args())
        self.assertEqual(first_result, 2)

        stdout = StringIO()
        with (
            patch("register_gcp_run._build_ledger", return_value=ledger),
            redirect_stdout(stdout),
        ):
            retry_result = register_gcp_run.main(self._args())

        self.assertEqual(retry_result, 0)
        self.assertEqual(store.create_calls, 2)
        self.assertEqual(len(store.objects), 1)
        self.assertEqual(json.loads(stdout.getvalue())["generation"], 23)

    def test_refuses_ledger_project_that_differs_from_the_manifest(self):
        stderr = StringIO()
        with (
            patch("register_gcp_run._build_ledger") as build,
            redirect_stderr(stderr),
        ):
            result = register_gcp_run.main(
                self._args(recovery_project_id="other-recovery-456")
            )

        self.assertEqual(result, 2)
        build.assert_not_called()
        self.assertIn("must match the recovery project", stderr.getvalue())

    def test_refuses_ledger_bucket_that_differs_from_the_manifest(self):
        stderr = StringIO()
        with (
            patch("register_gcp_run._build_ledger") as build,
            redirect_stderr(stderr),
        ):
            result = register_gcp_run.main(
                self._args(ledger_bucket="other-ledger-private")
            )

        self.assertEqual(result, 2)
        build.assert_not_called()
        self.assertIn("ledger bucket", stderr.getvalue())

    def test_only_accepts_an_explicit_authorized_user_credential_file(self):
        credential_path = Path(self.temporary.name) / "human.json"
        credential_path.write_text(
            json.dumps({"type": "service_account"}), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "authorized-user"):
            register_gcp_run._build_ledger(
                credentials_file=credential_path,
                ledger_bucket="portfolio-ledger-private",
                ledger_project_id="recovery-lab-456",
                expected_email="operator@example.com",
                expected_auditor_id="google-user-12345678901234567890",
            )

    def test_rejects_non_object_intent_before_constructing_cloud_access(self):
        self.intent_path.write_text("[]", encoding="utf-8")
        stderr = StringIO()
        with (
            patch("register_gcp_run._build_ledger") as build,
            redirect_stderr(stderr),
        ):
            result = register_gcp_run.main(self._args())

        self.assertEqual(result, 2)
        build.assert_not_called()
        self.assertIn("JSON object", stderr.getvalue())

    def test_rejects_invalid_preflight_before_constructing_cloud_access(self):
        self.document["finance"]["alerts_verified"] = False
        self.intent_path.write_text(json.dumps(self.document), encoding="utf-8")
        stderr = StringIO()
        with (
            patch("register_gcp_run._build_ledger") as build,
            redirect_stderr(stderr),
        ):
            result = register_gcp_run.main(self._args())

        self.assertEqual(result, 2)
        build.assert_not_called()
        self.assertIn("alerts_verified", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
