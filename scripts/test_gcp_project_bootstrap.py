"""Mocked resumable GCP project, billing, and API activation tests."""

from datetime import datetime, timedelta, timezone
import unittest

from gcp_project_bootstrap import GCPProjectBootstrapper
from gcp_project_lifecycle_adapter import BILLING_TYPE, PROJECT_TYPE, SERVICE_TYPE
from recovery_controller import ProviderReceipt
from run_ledger import RunLedger
from test_run_contract import RUN_ID
from test_run_ledger import FakeObjectStore, current_intent, prepare_control_bootstrap


PROJECT = "sample-lab-123"


class FakeFinancePreflight:
    def __init__(self, error=None):
        self.error = error
        self.calls = []
        self.budget_missing = False

    def verify(self, manifest, *, observed_at):
        self.calls.append((manifest["run_id"], observed_at))
        if self.error:
            raise self.error

    def verify_project_budget(self, manifest, *, observed_at):
        if self.error:
            raise self.error
        if self.budget_missing:
            return None
        return {
            "project_number": "123456789012",
            "budget_name": (
                f"{manifest['finance']['billing_account_id']}/budgets/test-budget"
            ),
            "observed_at": observed_at,
            "budget_response_sha256": "a" * 64,
            "alert_receipt_sha256": "b" * 64,
            "credit_receipt_sha256": "c" * 64,
            "verified_by": "google-user-test-subject",
        }


class FakeProjectProvider:
    def __init__(self):
        self.resources = set()
        self.submit_calls = []
        self.inspect_calls = []
        self.poll_calls = []
        self.lose_project_create_response = False
        self.async_service = None
        self.async_submitted = False

    def authorize_actor(self, lease_owner, action, resource_type):
        if action == "create" and lease_owner != "launcher":
            raise AssertionError("create operation did not use launcher lease")
        return None

    def submit(self, action, resource_type, resource_id, request):
        self.submit_calls.append((action, resource_type, resource_id, request))
        if action != "create":
            raise AssertionError("bootstrap submitted a non-create operation")
        self.resources.add((resource_type, resource_id))
        if resource_type == PROJECT_TYPE and self.lose_project_create_response:
            self.lose_project_create_response = False
            raise ConnectionError("simulated lost project-create response")
        if resource_type == SERVICE_TYPE and resource_id == self.async_service:
            self.async_submitted = True
            return ProviderReceipt("running", "a" * 64, "operations/service-1")
        return ProviderReceipt("succeeded", "b" * 64)

    def inspect_resource(self, resource_type, resource_id):
        self.inspect_calls.append((resource_type, resource_id))
        state = (
            "present" if (resource_type, resource_id) in self.resources else "absent"
        )
        return ProviderReceipt(state, "c" * 64)

    def poll_operation(self, operation_name):
        self.poll_calls.append(operation_name)
        return ProviderReceipt("succeeded", "d" * 64, operation_name)


class GCPProjectBootstrapperTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.ledger = RunLedger(FakeObjectStore(), clock=lambda: self.now)
        created = self.ledger.create(current_intent())
        leased = self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=created.generation,
            owner="launcher",
            now=self.now,
        )
        self.prepared = prepare_control_bootstrap(self.ledger, leased, self.now)
        self.intent_sha256 = self.prepared.metadata["intent_sha256"]
        self.provider = FakeProjectProvider()
        self.finance = FakeFinancePreflight()
        self.bootstrapper = GCPProjectBootstrapper(
            self.ledger,
            self.provider,
            self.finance,
            clock=lambda: self.now + timedelta(seconds=1),
        )

    def run_bootstrap(self):
        return self.bootstrapper.run(
            RUN_ID,
            confirm_project_id=PROJECT,
            confirm_intent_sha256=self.intent_sha256,
        )

    def test_happy_path_records_exact_project_billing_and_all_api_results(self):
        result = self.run_bootstrap()
        self.assertEqual(result.status, "ready_for_workload_provisioning")
        self.assertEqual(result.snapshot.manifest["state"], "provisioning")
        self.assertEqual(len(self.finance.calls), 1)
        self.assertEqual(
            sum(call[1] == PROJECT_TYPE for call in self.provider.submit_calls), 1
        )
        self.assertEqual(
            sum(call[1] == BILLING_TYPE for call in self.provider.submit_calls), 1
        )
        api_services = [
            call[2].rsplit("/", 1)[-1]
            for call in self.provider.submit_calls
            if call[1] == SERVICE_TYPE
        ]
        self.assertEqual(api_services, sorted(api_services))
        self.assertEqual(
            api_services,
            self.prepared.manifest["scope"]["enabled_api_services"],
        )
        stored = self.ledger.read(RUN_ID)
        self.assertIsNone(stored.metadata["lease"]["owner"])

    def test_wrong_project_or_registered_intent_confirmation_fails_before_lease(self):
        with self.assertRaisesRegex(RuntimeError, "exact run project"):
            self.bootstrapper.run(
                RUN_ID,
                confirm_project_id="another-lab-456",
                confirm_intent_sha256=self.intent_sha256,
            )
        with self.assertRaisesRegex(RuntimeError, "registered run intent"):
            self.bootstrapper.run(
                RUN_ID,
                confirm_project_id=PROJECT,
                confirm_intent_sha256="0" * 64,
            )
        self.assertEqual(self.provider.submit_calls, [])
        self.assertEqual(self.ledger.read(RUN_ID).manifest["state"], "prepared")

    def test_retry_flag_must_be_a_boolean(self):
        with self.assertRaisesRegex(RuntimeError, "explicit boolean"):
            self.bootstrapper.run(
                RUN_ID,
                confirm_project_id=PROJECT,
                confirm_intent_sha256=self.intent_sha256,
                retry_failed="yes",
            )
        self.assertEqual(self.provider.submit_calls, [])

    def test_finance_preflight_failure_prevents_even_project_inspection(self):
        self.bootstrapper.finance_preflight.error = RuntimeError("review rejected")
        with self.assertRaisesRegex(RuntimeError, "review rejected"):
            self.run_bootstrap()
        self.assertEqual(self.provider.inspect_calls, [])
        self.assertEqual(self.provider.submit_calls, [])
        self.assertEqual(self.ledger.read(RUN_ID).manifest["state"], "prepared")
        self.assertIsNone(self.ledger.read(RUN_ID).metadata["lease"]["owner"])

    def test_project_must_be_absent_at_activation_preflight(self):
        self.provider.resources.add((PROJECT_TYPE, f"projects/{PROJECT}"))
        result = self.run_bootstrap()
        self.assertEqual(result.status, "project_preflight_not_absent")
        self.assertEqual(result.snapshot.manifest["state"], "prepared")
        self.assertEqual(self.provider.submit_calls, [])

    def test_missing_run_budget_holds_before_any_run_api_is_enabled(self):
        self.finance.budget_missing = True

        waiting = self.run_bootstrap()

        self.assertEqual(
            waiting.status, "awaiting_project_budget"
        )
        self.assertEqual(waiting.snapshot.manifest["state"], "budget_verification")
        self.assertFalse(any(call[1] == SERVICE_TYPE for call in self.provider.submit_calls))
        self.assertIsNone(self.ledger.read(RUN_ID).metadata["lease"]["owner"])

    def test_lost_project_create_response_reconciles_without_duplicate_create(self):
        self.provider.lose_project_create_response = True
        uncertain = self.run_bootstrap()
        self.assertEqual(uncertain.status, "provider_response_uncertain")
        self.assertEqual(uncertain.snapshot.manifest["state"], "project_create_requested")
        self.assertEqual(len(self.provider.submit_calls), 1)

        resumed = self.run_bootstrap()
        self.assertEqual(resumed.status, "ready_for_workload_provisioning")
        self.assertEqual(
            sum(call[1] == PROJECT_TYPE for call in self.provider.submit_calls), 1
        )

    def test_async_api_enablement_is_polled_on_resume_without_resubmission(self):
        service = self.prepared.manifest["scope"]["enabled_api_services"][0]
        self.provider.async_service = f"projects/{PROJECT}/services/{service}"
        waiting = self.run_bootstrap()
        self.assertEqual(waiting.status, "waiting_for_provider_operation")
        self.assertEqual(waiting.snapshot.manifest["state"], "api_enablement")
        submitted = sum(call[1] == SERVICE_TYPE for call in self.provider.submit_calls)

        resumed = self.run_bootstrap()
        self.assertEqual(resumed.status, "ready_for_workload_provisioning")
        self.assertEqual(self.provider.poll_calls, ["operations/service-1"])
        self.assertEqual(
            sum(call[1] == SERVICE_TYPE for call in self.provider.submit_calls),
            submitted + len(self.prepared.manifest["scope"]["enabled_api_services"]) - 1,
        )

    def test_failed_operation_requires_explicit_retry_review(self):
        self.provider.submit = lambda *args: ProviderReceipt("failed", "e" * 64)
        result = self.run_bootstrap()
        self.assertEqual(result.status, "provider_operation_failed")
        self.assertEqual(result.snapshot.manifest["state"], "project_create_requested")
        self.assertEqual(len(self.provider.submit_calls), 0)


if __name__ == "__main__":
    unittest.main()
