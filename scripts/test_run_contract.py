"""Fail-closed checks for the credential-free GCP run contract."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import unittest

from run_contract import (
    ContractError,
    _audit_summary,
    cleanup_plan,
    control_cleanup_plan,
    next_state,
    operation_id,
    validate_intent,
    validate_manifest,
)


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
PROJECT = "sample-lab-123"
RUN_ID = "lab-20261018-a1b2"


def intent():
    return {
        "schema": "portfolio.gcp-run.v2",
        "run_id": RUN_ID,
        "scope": {
            "project_id": PROJECT,
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
            "zones": [
                "europe-west2-a",
                "europe-west2-b",
                "europe-west2-c",
            ],
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
            "net_cash_exposure_gbp": 0.0,
            "available_credit_gbp": 50,
            "credit_observed_at": "2026-10-18T11:55:00Z",
            "credit_expires_at": "2026-11-18T00:00:00Z",
            "monthly_alert_threshold_gbp": 70,
            "alerts_verified": True,
        },
        "cleanup": {
            "service_account": "lab-20261018-a1b2-reaper@recovery-lab-456.iam.gserviceaccount.com",
            "identity_verified": True,
            "independent_recovery_verified": True,
        },
        "terraform_state": {
            "backend": {
                "bucket": "run-state-lab-20261018-a1b2",
                "object": f"runs/{RUN_ID}/terraform.tfstate",
                "generation": None,
            },
            "backup": {
                "bucket": "portfolio-state-backup",
                "object": f"runs/{RUN_ID}/terraform.tfstate",
                "generation": None,
            },
            "restore_proofs": [],
        },
        "operations": [],
        "state": "control_bootstrapping",
        "resources": [],
        "controls": [
            {"type": "gcp.run.job", "id": "projects/recovery-lab-456/locations/europe-west2/jobs/lab-20261018-a1b2-reaper", "ownership_proof": "run_name", "status": "expected", "delete_attempts": 0},
            {"type": "gcp.scheduler.job", "id": "projects/recovery-lab-456/locations/europe-west2/jobs/lab-20261018-a1b2-expiry", "ownership_proof": "run_name", "status": "expected", "delete_attempts": 0},
            {"type": "gcp.iam.service_account", "id": "projects/recovery-lab-456/serviceAccounts/lab-20261018-a1b2-reaper@recovery-lab-456.iam.gserviceaccount.com", "ownership_proof": "run_name", "status": "expected", "delete_attempts": 0},
            {"type": "gcp.iam.service_account", "id": "projects/recovery-lab-456/serviceAccounts/lab-20261018-a1b2-scheduler@recovery-lab-456.iam.gserviceaccount.com", "ownership_proof": "run_name", "status": "expected", "delete_attempts": 0},
        ],
    }


def resource(resource_type, name, *, proof="run_name", status="present"):
    return {
        "type": resource_type,
        "id": f"projects/{PROJECT}/locations/europe-west2/{name}",
        "ownership_proof": proof,
        "status": status,
        "delete_attempts": 0,
    }


def audit_receipt(sequence, phase, *, checks=None, result="passed", complete=True):
    if checks is None:
        checks = (
            {"resource_inventory_complete": True, "resources_absent": True}
            if phase == "workload"
            else {
                "resource_inventory_complete": True, "resources_absent": True,
                "project_absent": True, "project_services_reconciled": True,
                "billing_unlinked": True,
                "budget_reconciled": True, "recovery_controls_absent": True,
            }
        )
    receipt = {
        "sequence": sequence,
        "run_id": RUN_ID,
        "project_id": PROJECT,
        "recovery_project_id": "recovery-lab-456",
        "phase": phase,
        "result": result,
        "complete": complete,
        "auditor_id": "google-user-12345678901234567890",
        "observed_at": f"2026-10-18T12:{sequence:02}:00Z",
        "inventory_sha256": "a" * 64,
        "evidence_sha256": "b" * 64,
        "manifest_generation": sequence,
        "checks": checks,
    }
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    receipt["receipt_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return receipt


def retirement_operations():
    operations = []
    targets = [
        ("gcp.billing.project_billing_info", "projects/" + PROJECT),
        ("gcp.resourcemanager.project", "projects/" + PROJECT),
    ]
    for attempt, (resource_type, resource_id) in enumerate(targets, start=1):
        action = "delete"
        request_sha256 = hashlib.sha256(b"{}").hexdigest()
        operation = operation_id(
            RUN_ID, 1, action, resource_type, resource_id,
            request_sha256, "project_retiring",
        )
        intent_event = {
            "sequence": len(operations) + 1,
            "kind": "intent",
            "operation_id": operation,
            "attempt": 1,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "request_sha256": request_sha256,
            "phase": "project_retiring",
            "at": f"2026-10-18T12:0{attempt}:00Z",
        }
        operations.append(intent_event)
        operations.append({
            "sequence": len(operations) + 1,
            "kind": "result",
            "operation_id": operation,
            "outcome": "succeeded",
            "provider_operation_name": None,
            "response_sha256": "c" * 64,
            "at": f"2026-10-18T12:0{attempt}:01Z",
        })
    return operations


class RunIntentTests(unittest.TestCase):
    def test_approved_london_three_zone_intent_passes(self):
        self.assertEqual(validate_intent(intent(), now=NOW)["state"], "control_bootstrapping")

    def test_credit_balance_can_be_zero_with_verified_runway_review(self):
        candidate = intent()
        candidate["finance"]["available_credit_gbp"] = 0
        self.assertEqual(validate_intent(candidate, now=NOW)["run_id"], RUN_ID)

    def test_billing_account_name_must_be_canonical(self):
        candidate = intent()
        candidate["finance"]["billing_account_id"] = "not-an-account"
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)

    def test_enabled_api_service_policy_must_be_sorted_unique_and_valid(self):
        for services in (
            ["compute.googleapis.com", "compute.googleapis.com"],
            ["compute.googleapis.com", "anthosconfigmanagement.googleapis.com"],
            ["not-an-api"],
        ):
            candidate = intent()
            candidate["scope"]["enabled_api_services"] = services
            with self.subTest(services=services), self.assertRaises(ContractError):
                validate_intent(candidate, now=NOW)

    def test_wrong_region_or_zone_is_rejected(self):
        for field, value in (
            ("region", "us-central1"),
            ("zones", ["europe-west2-a"] * 3),
            ("zones", [{"bad": "type"}, "europe-west2-b", "europe-west2-c"]),
        ):
            candidate = intent()
            candidate["scope"][field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_intent(candidate, now=NOW)

    def test_invalid_run_or_project_identifier_is_rejected(self):
        for field, value in (
            ("run_id", "lab-bad/../x"),
            ("run_id", "lab-" + "a" * 21),
        ):
            candidate = intent()
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_intent(candidate, now=NOW)
        candidate = intent()
        candidate["scope"]["project_id"] = "other-project!"
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)

    def test_state_backup_references_are_distinct_exact_and_generation_safe(self):
        candidate = intent()
        self.assertEqual(validate_intent(candidate, now=NOW)["run_id"], RUN_ID)
        candidate["terraform_state"]["backup"]["bucket"] = (
            candidate["terraform_state"]["backend"]["bucket"]
        )
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)

        for role, field, value in (
            ("backend", "object", "runs/another-run/terraform.tfstate"),
            ("backup", "generation", True),
            ("backend", "bucket", "../unsafe"),
            ("backup", "bucket", "goog-state-backup"),
            ("backup", "bucket", "google-state-backup"),
        ):
            candidate = intent()
            candidate["terraform_state"][role][field] = value
            with self.subTest(role=role, field=field), self.assertRaises(ContractError):
                validate_intent(candidate, now=NOW)

    def test_state_restore_proofs_validate_generation_and_crc32c_evidence(self):
        candidate = intent()
        proof = {
            "snapshot": "current",
            "state_generation": "12",
            "backup_generation": "5",
            "restored_generation": "8",
            "verified_at": "2026-10-18T12:10:00Z",
            "result": "passed",
            "state_crc32c": "ImIEBA==",
            "backup_crc32c": "ImIEBA==",
            "restored_crc32c": "ImIEBA==",
        }
        candidate["terraform_state"]["restore_proofs"] = [proof]
        self.assertEqual(validate_intent(candidate, now=NOW)["run_id"], RUN_ID)
        candidate["terraform_state"]["restore_proofs"][0]["restored_crc32c"] = "ImIEBw=="
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)

    def test_provider_operation_intent_and_result_are_exact_and_deterministic(self):
        candidate = intent()
        resource_id = f"projects/{PROJECT}/global/networks/{RUN_ID}-network"
        candidate["state"] = "provisioning"
        candidate["resources"] = [
            {
                "type": "gcp.compute.network",
                "id": resource_id,
                "ownership_proof": "run_name",
                "status": "expected",
                "delete_attempts": 0,
            }
        ]
        for control in candidate["controls"]:
            control["status"] = "present"
        request_sha = "c" * 64
        phase = "provisioning"
        op_id = operation_id(
            RUN_ID, 1, "create", "gcp.compute.network", resource_id, request_sha, phase
        )
        candidate["operations"] = [
            {
                "sequence": 1,
                "kind": "intent",
                "operation_id": op_id,
                "attempt": 1,
                "action": "create",
                "resource_type": "gcp.compute.network",
                "resource_id": resource_id,
                "request_sha256": request_sha,
                "phase": phase,
                "at": "2026-10-18T12:01:00Z",
            },
            {
                "sequence": 2,
                "kind": "progress",
                "operation_id": op_id,
                "provider_operation_name": f"projects/{PROJECT}/regions/europe-west2/operations/op-123",
                "response_sha256": "e" * 64,
                "at": "2026-10-18T12:01:30Z",
            },
            {
                "sequence": 3,
                "kind": "result",
                "operation_id": op_id,
                "outcome": "reconciled_present",
                "provider_operation_name": f"projects/{PROJECT}/regions/europe-west2/operations/op-123",
                "response_sha256": "d" * 64,
                "at": "2026-10-18T12:02:00Z",
            },
        ]
        self.assertEqual(validate_manifest(candidate)["run_id"], RUN_ID)

        changed_operation_name = deepcopy(candidate)
        changed_operation_name["operations"][1]["provider_operation_name"] += "-changed"
        with self.assertRaises(ContractError):
            validate_manifest(changed_operation_name)

        candidate["operations"][0]["resource_id"] = (
            "projects/other-project/global/networks/" + RUN_ID + "-network"
        )
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_unresolved_or_nondeterministic_provider_intent_fails_closed(self):
        candidate = intent()
        resource_id = f"projects/{PROJECT}/global/networks/{RUN_ID}-network"
        request_sha = "c" * 64
        phase = "provisioning"
        first = {
            "sequence": 1,
            "kind": "intent",
            "operation_id": operation_id(
                RUN_ID, 1, "create", "gcp.compute.network", resource_id, request_sha, phase
            ),
            "attempt": 1,
            "action": "create",
            "resource_type": "gcp.compute.network",
            "resource_id": resource_id,
            "request_sha256": request_sha,
            "phase": phase,
            "at": "2026-10-18T12:01:00Z",
        }
        second = dict(first, sequence=2, attempt=2, at="2026-10-18T12:02:00Z")
        second["operation_id"] = operation_id(
            RUN_ID, 2, "create", "gcp.compute.network", resource_id, request_sha, phase
        )
        candidate["operations"] = [first, second]
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

        candidate["operations"] = [dict(first, operation_id="op-" + "0" * 64)]
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_operation_requires_exact_inventory_and_bounded_attempt(self):
        candidate = intent()
        resource_id = f"projects/{PROJECT}/global/networks/{RUN_ID}-network"
        candidate["state"] = "provisioning"
        candidate["resources"] = [
            {
                "type": "gcp.compute.network",
                "id": resource_id,
                "ownership_proof": "run_name",
                "status": "expected",
                "delete_attempts": 0,
            }
        ]
        for control in candidate["controls"]:
            control["status"] = "present"
        event = {
            "sequence": 1,
            "kind": "intent",
            "operation_id": operation_id(
                RUN_ID, 1, "create", "gcp.compute.network", resource_id,
                "c" * 64, "provisioning",
            ),
            "attempt": 1,
            "action": "create",
            "resource_type": "gcp.compute.network",
            "resource_id": resource_id,
            "request_sha256": "c" * 64,
            "phase": "provisioning",
            "at": "2026-10-18T12:01:00Z",
        }
        candidate["operations"] = [event]

        missing_inventory = deepcopy(candidate)
        missing_inventory["resources"] = []
        with self.assertRaises(ContractError):
            validate_manifest(missing_inventory)

        over_attempted = deepcopy(candidate)
        over_attempted["operations"][0]["attempt"] = 4
        over_attempted["operations"][0]["operation_id"] = operation_id(
            RUN_ID, 4, "create", "gcp.compute.network", resource_id,
            "c" * 64, "provisioning",
        )
        with self.assertRaises(ContractError):
            validate_manifest(over_attempted)

    def test_deadline_must_be_future_and_within_sixty_minutes(self):
        for value in ("2026-10-18T11:59:59Z", "2026-10-18T13:00:01Z"):
            candidate = intent()
            candidate["deadline_at"] = value
            with self.subTest(deadline=value), self.assertRaises(ContractError):
                validate_intent(candidate, now=NOW)

    def test_timestamps_require_timezone_and_stale_credit_is_rejected(self):
        candidate = intent()
        candidate["created_at"] = "2026-10-18T12:00:00"
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)
        candidate = intent()
        candidate["finance"]["credit_expires_at"] = "2026-10-18T12:40:00Z"
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)

    def test_non_finite_boolean_and_negative_financial_values_fail(self):
        for value in (float("nan"), float("inf"), True, 0, -1, 10**10000):
            candidate = intent()
            candidate["finance"]["gross_estimate_gbp"] = value
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_intent(candidate, now=NOW)
        candidate = intent()
        candidate["finance"]["available_credit_gbp"] = True
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)

    def test_no_alert_or_independent_recovery_means_no_activation(self):
        for section, key in (("finance", "alerts_verified"),
                             ("cleanup", "independent_recovery_verified")):
            candidate = intent()
            candidate[section][key] = False
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate_intent(candidate, now=NOW)

    def test_unknown_intent_fields_fail_closed(self):
        candidate = intent()
        candidate["operator_command"] = "delete-everything"
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)

    def test_recovery_controls_fail_closed_for_wrong_location_or_malformed_status(self):
        candidate = intent()
        candidate["controls"][0]["id"] = candidate["controls"][0]["id"].replace(
            "europe-west2", "us-central1"
        )
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)
        candidate = intent()
        candidate["controls"][0]["status"] = {"unexpected": "object"}
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)

    def test_new_intent_cannot_predeclare_resources_or_skip_control_bootstrap(self):
        candidate = intent()
        candidate["state"] = "prepared"
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)
        candidate = intent()
        candidate["audit_receipts"] = [{"fabricated": True}]
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)
        candidate = intent()
        candidate["resources"] = [resource("gcp.gke.cluster", RUN_ID)]
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)
        candidate = intent()
        candidate["controls"] = candidate["controls"][:-1]
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)
        candidate = intent()
        candidate["controls"][0]["id"] = (
            "projects/other-project/locations/europe-west2/jobs/"
            + RUN_ID + "-reaper"
        )
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)


class ManifestTests(unittest.TestCase):
    def manifest(self):
        candidate = intent()
        candidate["state"] = "cleaning"
        candidate["resources"] = [
            resource("gcp.compute.network", RUN_ID + "-network"),
            resource("gcp.gke.cluster", RUN_ID + "-cluster"),
            resource("gcp.compute.router_nat", RUN_ID + "-nat"),
            resource("gcp.gke.node_pool", RUN_ID + "-cluster/nodePools/default"),
            resource("gcp.gkehub.membership", RUN_ID + "-membership"),
            resource("gcp.gkehub.feature_membership", RUN_ID + "-config"),
            {
                "type": "gcp.gkehub.feature",
                "id": f"projects/{PROJECT}/locations/global/features/configmanagement",
                "ownership_proof": "run_project",
                "status": "present",
                "delete_attempts": 0,
            },
        ]
        return candidate

    def test_manifest_accepts_run_scoped_resource_inventory(self):
        self.assertEqual(validate_manifest(self.manifest())["state"], "cleaning")

    def test_reaper_identity_is_recovery_project_scoped(self):
        candidate = self.manifest()
        candidate["cleanup"]["service_account"] = (
            f"janitor@{PROJECT}.iam.gserviceaccount.com"
        )
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_persisted_manifest_cannot_switch_to_another_recovery_identity(self):
        candidate = self.manifest()
        candidate["cleanup"]["service_account"] = (
            "other@recovery-lab-456.iam.gserviceaccount.com"
        )
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_reaper_identity_must_match_predeclared_control_account(self):
        candidate = intent()
        candidate["cleanup"]["service_account"] = (
            "other@recovery-lab-456.iam.gserviceaccount.com"
        )
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)

    def test_prepared_requires_all_recovery_controls_verified(self):
        candidate = self.manifest()
        candidate["state"] = "prepared"
        candidate["resources"] = []
        with self.assertRaises(ContractError):
            validate_manifest(candidate)
        for control in candidate["controls"]:
            control["status"] = "present"
        self.assertEqual(validate_manifest(candidate)["state"], "prepared")

    def test_unknown_persistent_or_project_wide_resource_is_rejected(self):
        for candidate_type in (
            "gcp.storage.bucket",
            "gcp.iam.workload_identity_pool",
            "gcp.iam.project_binding",
            "gcp.iam.service_account",
            "gcp.project.service",
            "gcp.compute.network",
        ):
            candidate = self.manifest()
            candidate["resources"][0] = resource(candidate_type, "shared")
            with self.subTest(resource_type=candidate_type), self.assertRaises(ContractError):
                validate_manifest(candidate)

    def test_resource_must_match_project_run_and_unique_ownership(self):
        candidate = self.manifest()
        candidate["resources"][0]["id"] = "projects/other-project/networks/x"
        with self.assertRaises(ContractError):
            validate_manifest(candidate)
        candidate = self.manifest()
        candidate["resources"][0]["id"] = f"projects/{PROJECT}/networks/shared"
        with self.assertRaises(ContractError):
            validate_manifest(candidate)
        candidate = self.manifest()
        candidate["resources"][0]["ownership_proof"] = "manifest_record"
        candidate["resources"][0]["id"] = f"projects/{PROJECT}/networks/shared"
        with self.assertRaises(ContractError):
            validate_manifest(candidate)
        candidate = self.manifest()
        candidate["resources"].append(deepcopy(candidate["resources"][0]))
        with self.assertRaises(ContractError):
            validate_manifest(candidate)
        candidate = self.manifest()
        candidate["resources"][0]["id"] = (
            f"projects/other-{PROJECT}/networks/{RUN_ID}-network"
        )
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_regional_resources_must_match_the_approved_london_region(self):
        candidate = self.manifest()
        candidate["resources"][1]["id"] = (
            f"projects/{PROJECT}/locations/us-central1/{RUN_ID}-cluster"
        )
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_malformed_manifest_state_and_audit_result_fail_closed(self):
        candidate = self.manifest()
        candidate["state"] = ["cleaning"]
        with self.assertRaises(ContractError):
            validate_manifest(candidate)
        candidate = self.manifest()
        candidate["independent_audit"] = {"result": [], "complete": True}
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_unknown_ownership_or_delete_attempt_count_fails(self):
        candidate = self.manifest()
        candidate["resources"][0]["ownership_proof"] = "project_search"
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_storage_object_id_binds_run_project_bucket_name_and_generation(self):
        candidate = self.manifest()
        object_id = (
            f"projects/{PROJECT}/buckets/{RUN_ID}-objects/objects/"
            "reports%2Fgolden.json@123"
        )
        candidate["resources"].append({
            "type": "gcp.storage.object",
            "id": object_id,
            "ownership_proof": "run_name",
            "status": "present",
            "delete_attempts": 0,
        })
        self.assertIn(object_id, {
            item["id"] for item in validate_manifest(candidate)["resources"]
        })

        candidate["resources"][-1]["id"] = (
            f"projects/other-lab-456/buckets/{RUN_ID}-objects/objects/"
            "reports%2Fgolden.json@123"
        )
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

        candidate["resources"][-1]["id"] = (
            f"projects/{PROJECT}/buckets/{RUN_ID}-objects/objects/"
            "reports%2fgolden.json@123"
        )
        with self.assertRaises(ContractError):
            validate_manifest(candidate)
        candidate = self.manifest()
        candidate["resources"][0]["delete_attempts"] = True
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_discovery_receipt_sequence_rejects_boolean_integer(self):
        candidate = self.manifest()
        candidate["state"] = "cleaning"
        receipt = {
            "sequence": 1,
            "run_id": RUN_ID,
            "project_id": PROJECT,
            "auditor_id": "google-user-12345678901234567890",
            "observed_at": "2026-10-18T12:01:00Z",
            "manifest_sha256": "a" * 64,
            "evidence_sha256": "b" * 64,
            "registered_resources": [],
            "unregistered_resource_count": 0,
            "unregistered_resources_sha256": "c" * 64,
            "operation_count": 0,
            "manifest_generation": 1,
        }
        canonical = json.dumps(
            receipt, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        receipt["receipt_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        candidate["discovery_receipts"] = [receipt]
        self.assertEqual(validate_manifest(candidate)["discovery_receipts"][0]["sequence"], 1)
        receipt["sequence"] = True
        canonical = json.dumps(
            {key: value for key, value in receipt.items() if key != "receipt_id"},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        receipt["receipt_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_cleanup_is_reverse_dependency_order_and_stable(self):
        result = cleanup_plan(self.manifest())
        ranks = [
            {
                "gcp.gkehub.feature_membership": 190,
                "gcp.gkehub.membership": 180,
                "gcp.gkehub.feature": 170,
                "gcp.gke.node_pool": 160,
                "gcp.gke.cluster": 150,
                "gcp.compute.router_nat": 110,
                "gcp.compute.network": 80,
            }[item["type"]]
            for item in result
        ]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        self.assertEqual(result, cleanup_plan(self.manifest()))

    def test_cleanup_requires_teardown_and_never_includes_already_deleted(self):
        candidate = self.manifest()
        candidate["state"] = "ready"
        with self.assertRaises(ContractError):
            cleanup_plan(candidate)
        candidate["state"] = "cleaning"
        candidate["resources"][0]["status"] = "deleted"
        self.assertNotIn(candidate["resources"][0], cleanup_plan(candidate))

    def test_closed_requires_complete_independent_audit_and_empty_inventory(self):
        candidate = self.manifest()
        candidate["state"] = "closed"
        candidate["operations"] = retirement_operations()
        for item in candidate["resources"]:
            item["status"] = "deleted"
        for item in candidate["controls"]:
            item["status"] = "deleted"
        workload = audit_receipt(1, "workload")
        closure = audit_receipt(2, "closure")
        candidate["audit_receipts"] = [workload, closure]
        candidate["independent_audit"] = _audit_summary(closure)
        self.assertEqual(validate_manifest(candidate)["state"], "closed")
        candidate["audit_receipts"][-1]["checks"]["project_absent"] = False
        with self.assertRaises(ContractError):
            validate_manifest(candidate)
        candidate["audit_receipts"] = [workload]
        candidate["independent_audit"] = _audit_summary(workload)
        candidate["resources"][0]["status"] = "present"
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_bootstrap_or_ledger_resources_are_never_in_run_cleanup(self):
        candidate = self.manifest()
        candidate["resources"].append(
            resource("gcp.scheduler.job", RUN_ID + "-scheduler")
        )
        candidate["resources"].append(
            resource("gcp.run.job", RUN_ID + "-reaper")
        )
        with self.assertRaises(ContractError):
            cleanup_plan(candidate)
        candidate = self.manifest()
        plan = cleanup_plan(candidate)
        self.assertNotIn("gcp.scheduler.job", [item["type"] for item in plan])
        candidate["state"] = "controls_reclaiming"
        candidate["operations"] = retirement_operations()
        with self.assertRaises(ContractError):
            control_cleanup_plan(candidate)
        for item in candidate["resources"]:
            item["status"] = "deleted"
        workload = audit_receipt(1, "workload")
        candidate["audit_receipts"] = [workload]
        candidate["independent_audit"] = _audit_summary(workload)
        controls = control_cleanup_plan(candidate)
        self.assertEqual(
            [item["type"] for item in controls],
            [
                "gcp.scheduler.job",
                "gcp.run.job",
                "gcp.iam.service_account",
                "gcp.iam.service_account",
            ],
        )
        candidate["resources"].append(
            resource("gcp.storage.bucket", RUN_ID + "-ledger")
        )
        with self.assertRaises(ContractError):
            cleanup_plan(candidate)

    def test_state_transitions_follow_project_and_reclamation_phases(self):
        self.assertEqual(
            next_state("prepared", "project_create_requested"),
            "project_create_requested",
        )
        self.assertEqual(next_state("incomplete", "cleaning"), "cleaning")
        self.assertEqual(
            next_state("auditing", "project_retiring"),
            "project_retiring",
        )
        self.assertEqual(
            next_state("project_retiring", "controls_reclaiming"),
            "controls_reclaiming",
        )
        with self.assertRaises(ContractError):
            next_state("auditing", "controls_reclaiming")
        with self.assertRaises(ContractError):
            next_state("closed", "cleaning")
        with self.assertRaises(ContractError):
            next_state("ready", "provisioning")

    def test_project_delete_must_follow_billing_unlink_result(self):
        candidate = intent()
        candidate["state"] = "project_retiring"
        receipt = audit_receipt(1, "workload")
        candidate["audit_receipts"] = [receipt]
        candidate["independent_audit"] = _audit_summary(receipt)
        operations = retirement_operations()
        reordered = operations[2:] + operations[:2]
        for index, event in enumerate(reordered, start=1):
            event["sequence"] = index
            event["at"] = f"2026-10-18T12:0{index}:00Z"
        candidate["operations"] = reordered
        with self.assertRaisesRegex(ContractError, "prior successful billing unlink"):
            validate_manifest(candidate)


if __name__ == "__main__":
    unittest.main()
