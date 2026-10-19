"""Local contracts for the Cloud Run post-audit retirement entrypoint."""

from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
import unittest

from gcp_reaper_entrypoint import (
    ReaperEntrypointError,
    _verify_manifest_binding,
    execute_project_retirement,
    main,
    parse_job_config,
)
from run_contract import _audit_summary
from test_run_contract import intent


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
RUN_ID = "lab-20261018-a1b2"
PROJECT_ID = "sample-lab-123"
RECOVERY_PROJECT_ID = "recovery-lab-456"
EMAIL = f"{RUN_ID}-reaper@{RECOVERY_PROJECT_ID}.iam.gserviceaccount.com"


def env_values():
    return {
        "PORTFOLIO_RUN_ID": RUN_ID,
        "PORTFOLIO_PROJECT_ID": PROJECT_ID,
        "PORTFOLIO_RECOVERY_PROJECT_ID": RECOVERY_PROJECT_ID,
        "PORTFOLIO_RUN_PARENT": "folders/123456",
        "PORTFOLIO_BILLING_ACCOUNT": "billingAccounts/ABCDEF-123456-GHIJKL",
        "PORTFOLIO_LEDGER_BUCKET": "portfolio-ledger-private",
        "PORTFOLIO_ENABLED_SERVICES": json.dumps(intent()["scope"]["enabled_api_services"]),
        "PORTFOLIO_REAPER_EMAIL": EMAIL,
        "PORTFOLIO_REAPER_SERVICE_ACCOUNT_ID": "12345678901234567890",
        "PORTFOLIO_IDENTITY_AUDIENCE": "portfolio-reaper-v1",
    }


def retirement_manifest():
    manifest = deepcopy(intent())
    manifest["state"] = "project_retiring"
    manifest["controls"] = [
        {**control, "status": "present"}
        for control in manifest["controls"]
    ]
    receipt = {
        "sequence": 1,
        "run_id": RUN_ID,
        "project_id": PROJECT_ID,
        "recovery_project_id": RECOVERY_PROJECT_ID,
        "phase": "workload",
        "result": "passed",
        "complete": True,
        "auditor_id": "google-user-independent-123",
        "observed_at": "2026-10-19T11:00:00Z",
        "inventory_sha256": "a" * 64,
        "evidence_sha256": "b" * 64,
        "manifest_generation": 4,
        "checks": {"resource_inventory_complete": True, "resources_absent": True},
    }
    receipt["receipt_id"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest["audit_receipts"] = [receipt]
    manifest["independent_audit"] = _audit_summary(receipt)
    return manifest


class FakeLedger:
    def __init__(self, manifest):
        self.snapshot = SimpleNamespace(manifest=manifest, generation=8)
        self.acquired = []
        self.replaced = []

    def read(self, run_id):
        assert run_id == RUN_ID
        return self.snapshot

    def acquire_lease(self, run_id, **kwargs):
        self.acquired.append((run_id, kwargs))
        return self.snapshot

    def replace(self, run_id, **kwargs):
        self.replaced.append((run_id, kwargs))
        candidate = kwargs["manifest"]
        self.snapshot = SimpleNamespace(manifest=candidate, generation=9)
        return self.snapshot


class FakeController:
    def __init__(self, states=("succeeded", "succeeded")):
        self.states = iter(states)
        self.calls = []

    def execute(self, run_id, **kwargs):
        self.calls.append((run_id, kwargs))
        return SimpleNamespace(state=next(self.states))


class GCPReaperEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.config = parse_job_config(env_values())

    def test_job_configuration_is_strict_and_sorted(self):
        values = env_values()
        values["PORTFOLIO_ENABLED_SERVICES"] = json.dumps(["z.googleapis.com", "a.googleapis.com"])
        with self.assertRaisesRegex(ReaperEntrypointError, "sorted unique"):
            parse_job_config(values)

    def test_missing_execution_id_fails_before_runtime_construction(self):
        constructed = []
        with self.assertRaisesRegex(ReaperEntrypointError, "execution ID"):
            main(env_values(), runtime_factory=lambda config: constructed.append(config))
        self.assertEqual(constructed, [])

    def test_manifest_mismatch_is_rejected(self):
        manifest = retirement_manifest()
        foreign_config = replace(self.config, project_id="foreign-lab-456")
        with self.assertRaisesRegex(ReaperEntrypointError, "does not match"):
            _verify_manifest_binding(manifest, foreign_config)

    def test_not_retiring_state_does_not_acquire_lease_or_mutate(self):
        manifest = deepcopy(intent())
        ledger = FakeLedger(manifest)
        controller = FakeController()
        outcome = execute_project_retirement(
            SimpleNamespace(ledger=ledger, controller=controller),
            self.config,
            execution_id="run-abc-123",
            now=NOW,
        )
        self.assertEqual(outcome, "waiting_for_independent_audit_or_prior_phase")
        self.assertEqual(ledger.acquired, [])
        self.assertEqual(controller.calls, [])

    def test_pre_deadline_does_not_acquire_lease_or_mutate(self):
        ledger = FakeLedger(retirement_manifest())
        controller = FakeController()
        outcome = execute_project_retirement(
            SimpleNamespace(ledger=ledger, controller=controller),
            self.config,
            execution_id="run-abc-123",
            now=datetime(2026, 10, 18, 12, 49, tzinfo=timezone.utc),
        )
        self.assertEqual(outcome, "before_run_deadline")
        self.assertEqual(ledger.acquired, [])
        self.assertEqual(controller.calls, [])

    def test_retirement_unlinks_billing_before_deleting_exact_project(self):
        ledger = FakeLedger(retirement_manifest())
        controller = FakeController()
        outcome = execute_project_retirement(
            SimpleNamespace(ledger=ledger, controller=controller),
            self.config,
            execution_id="run-abc-123",
            now=NOW,
        )
        self.assertEqual(
            outcome,
            "project_absence_reconciled_controls_audit_required",
        )
        self.assertEqual(
            [call[1]["resource_type"] for call in controller.calls],
            ["gcp.billing.project_billing_info", "gcp.resourcemanager.project"],
        )
        self.assertTrue(
            all(call[1]["resource_id"] == f"projects/{PROJECT_ID}" for call in controller.calls)
        )
        self.assertEqual(ledger.acquired[0][1]["owner"],
                         "janitor:google-service-account-12345678901234567890:run-abc-123")

    def test_failed_billing_unlink_stops_before_project_delete(self):
        ledger = FakeLedger(retirement_manifest())
        controller = FakeController(("failed",))
        outcome = execute_project_retirement(
            SimpleNamespace(ledger=ledger, controller=controller),
            self.config,
            execution_id="run-abc-123",
            now=NOW,
        )
        self.assertEqual(outcome, "billing_unlink_pending_or_incomplete")
        self.assertEqual(len(controller.calls), 1)
        self.assertEqual(controller.calls[0][1]["resource_type"], "gcp.billing.project_billing_info")


if __name__ == "__main__":
    unittest.main()
