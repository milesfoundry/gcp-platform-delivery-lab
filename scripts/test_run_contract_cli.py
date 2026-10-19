"""CLI tests verify dry-run output and fail-closed input handling."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from run_contract_cli import main


def manifest():
    project = "sample-lab-123"
    run_id = "lab-20261018-a1b2"
    return {
        "schema": "portfolio.gcp-run.v2",
        "run_id": run_id,
        "scope": {
            "project_id": project,
            "recovery_project_id": "recovery-lab-456",
            "region": "europe-west2",
            "enabled_api_services": [
                "anthosconfigmanagement.googleapis.com",
                "cloudresourcemanager.googleapis.com",
                "compute.googleapis.com",
                "container.googleapis.com",
                "gkeconnect.googleapis.com",
                "gkehub.googleapis.com",
                "iam.googleapis.com",
                "iamcredentials.googleapis.com",
                "logging.googleapis.com",
                "monitoring.googleapis.com",
                "pubsub.googleapis.com",
                "serviceusage.googleapis.com",
                "storage.googleapis.com",
            ],
            "zones": ["europe-west2-a", "europe-west2-b", "europe-west2-c"],
        },
        "created_at": "2026-10-18T12:00:00Z",
        "deadline_at": "2026-10-18T12:50:00Z",
        "source": {
            "revision": "a" * 40,
            "image_digest": "ghcr.io/milesfoundry/report-workshop@sha256:" + "b" * 64,
        },
        "finance": {
            "currency": "GBP",
            "billing_account_id": "billingAccounts/ABCDEF-123456-GHIJKL",
            "gross_estimate_gbp": 4.25,
            "net_cash_exposure_gbp": 0,
            "available_credit_gbp": 50,
            "credit_observed_at": "2026-10-18T11:55:00Z",
            "credit_expires_at": "2026-11-18T00:00:00Z",
            "monthly_alert_threshold_gbp": 70,
            "alerts_verified": True,
        },
        "cleanup": {
            "service_account": f"{run_id}-reaper@recovery-lab-456.iam.gserviceaccount.com",
            "identity_verified": True,
            "independent_recovery_verified": True,
        },
        "terraform_state": {
            "backend": {
                "bucket": "run-state-lab-20261018-a1b2",
                "object": f"runs/{run_id}/terraform.tfstate",
                "generation": None,
            },
            "backup": {
                "bucket": "portfolio-state-backup",
                "object": f"runs/{run_id}/terraform.tfstate",
                "generation": None,
            },
            "restore_proofs": [],
        },
        "operations": [],
        "state": "cleaning",
        "controls": [
            {"type": "gcp.run.job", "id": f"projects/recovery-lab-456/locations/europe-west2/jobs/{run_id}-reaper", "ownership_proof": "run_name", "status": "expected", "delete_attempts": 0},
            {"type": "gcp.scheduler.job", "id": f"projects/recovery-lab-456/locations/europe-west2/jobs/{run_id}-expiry", "ownership_proof": "run_name", "status": "expected", "delete_attempts": 0},
            {"type": "gcp.iam.service_account", "id": f"projects/recovery-lab-456/serviceAccounts/{run_id}-reaper@recovery-lab-456.iam.gserviceaccount.com", "ownership_proof": "run_name", "status": "expected", "delete_attempts": 0},
            {"type": "gcp.iam.service_account", "id": f"projects/recovery-lab-456/serviceAccounts/{run_id}-scheduler@recovery-lab-456.iam.gserviceaccount.com", "ownership_proof": "run_name", "status": "expected", "delete_attempts": 0},
        ],
        "resources": [
            {
                "type": "gcp.compute.network",
                "id": f"projects/{project}/global/networks/{run_id}",
                "ownership_proof": "run_name",
                "status": "present",
                "delete_attempts": 0,
            }
        ],
    }


class RunContractCliTests(unittest.TestCase):
    def invoke(self, mode, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main([mode, str(path)])
            return code, stdout.getvalue(), stderr.getvalue()

    def test_cleanup_mode_emits_deterministic_dry_run_ids(self):
        code, stdout, stderr = self.invoke("cleanup", manifest())
        self.assertEqual((code, stderr), (0, ""))
        result = json.loads(stdout)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["mode"], "cleanup")
        self.assertEqual(result["operations"][0]["type"], "gcp.compute.network")

    def test_invalid_json_contract_is_rejected_without_operations(self):
        candidate = manifest()
        candidate["run_id"] = "../../another-project"
        code, stdout, stderr = self.invoke("intent", candidate)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("run contract rejected input", stderr)


if __name__ == "__main__":
    unittest.main()
