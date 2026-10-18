"""Fail-closed checks for the credential-free GCP run contract."""

from copy import deepcopy
from datetime import datetime, timezone
import unittest

from run_contract import (
    ContractError,
    cleanup_plan,
    control_cleanup_plan,
    next_state,
    validate_intent,
    validate_manifest,
)


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
PROJECT = "sample-lab-123"
RUN_ID = "lab-20261018-a1b2"


def intent():
    return {
        "schema": "portfolio.gcp-run.v1",
        "run_id": RUN_ID,
        "scope": {
            "project_id": PROJECT,
            "region": "europe-west2",
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
            "gross_estimate_gbp": 4.25,
            "net_cash_exposure_gbp": 0.0,
            "available_credit_gbp": 50,
            "credit_observed_at": "2026-10-18T11:55:00Z",
            "credit_expires_at": "2026-11-18T00:00:00Z",
            "monthly_alert_threshold_gbp": 70,
            "alerts_verified": True,
        },
        "cleanup": {
            "service_account": "janitor@sample-lab-123.iam.gserviceaccount.com",
            "identity_verified": True,
            "independent_recovery_verified": True,
        },
        "state": "prepared",
        "resources": [],
    }


def resource(resource_type, name, *, proof="run_name", status="present"):
    return {
        "type": resource_type,
        "id": f"projects/{PROJECT}/locations/europe-west2/{name}",
        "ownership_proof": proof,
        "status": status,
        "delete_attempts": 0,
    }


class RunIntentTests(unittest.TestCase):
    def test_approved_london_three_zone_intent_passes(self):
        self.assertEqual(validate_intent(intent(), now=NOW)["state"], "prepared")

    def test_credit_balance_can_be_zero_with_verified_runway_review(self):
        candidate = intent()
        candidate["finance"]["available_credit_gbp"] = 0
        self.assertEqual(validate_intent(candidate, now=NOW)["run_id"], RUN_ID)

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

    def test_new_intent_cannot_predeclare_resources_or_skip_prepared(self):
        candidate = intent()
        candidate["state"] = "provisioning"
        with self.assertRaises(ContractError):
            validate_intent(candidate, now=NOW)
        candidate = intent()
        candidate["resources"] = [resource("gcp.gke.cluster", RUN_ID)]
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
        ]
        return candidate

    def test_manifest_accepts_run_scoped_resource_inventory(self):
        self.assertEqual(validate_manifest(self.manifest())["state"], "cleaning")

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

    def test_unknown_ownership_or_delete_attempt_count_fails(self):
        candidate = self.manifest()
        candidate["resources"][0]["ownership_proof"] = "project_search"
        with self.assertRaises(ContractError):
            validate_manifest(candidate)
        candidate = self.manifest()
        candidate["resources"][0]["delete_attempts"] = True
        with self.assertRaises(ContractError):
            validate_manifest(candidate)

    def test_cleanup_is_reverse_dependency_order_and_stable(self):
        result = cleanup_plan(self.manifest())
        ranks = [
            {
                "gcp.gkehub.feature_membership": 170,
                "gcp.gkehub.membership": 160,
                "gcp.gke.node_pool": 150,
                "gcp.gke.cluster": 140,
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
        for item in candidate["resources"]:
            item["status"] = "deleted"
        candidate["independent_audit"] = {"result": "passed", "complete": True}
        self.assertEqual(validate_manifest(candidate)["state"], "closed")
        for audit in (
            {"result": "passed", "complete": False},
            {"result": "incomplete", "complete": True},
        ):
            candidate["independent_audit"] = audit
            with self.subTest(audit=audit), self.assertRaises(ContractError):
                validate_manifest(candidate)
        candidate["independent_audit"] = {"result": "passed", "complete": True}
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
        candidate["resources"].pop()
        plan = cleanup_plan(candidate)
        self.assertNotIn("gcp.scheduler.job", [item["type"] for item in plan])
        candidate["state"] = "auditing"
        with self.assertRaises(ContractError):
            control_cleanup_plan(candidate)
        for item in candidate["resources"]:
            if item["type"] not in {"gcp.scheduler.job", "gcp.run.job"}:
                item["status"] = "deleted"
        candidate["independent_audit"] = {"result": "passed", "complete": True}
        controls = control_cleanup_plan(candidate)
        self.assertEqual(
            [item["type"] for item in controls],
            ["gcp.scheduler.job"],
        )
        candidate["resources"].append(
            resource("gcp.storage.bucket", RUN_ID + "-ledger")
        )
        with self.assertRaises(ContractError):
            cleanup_plan(candidate)

    def test_state_transitions_are_monotonic_except_explicit_recovery(self):
        self.assertEqual(next_state("prepared", "provisioning"), "provisioning")
        self.assertEqual(next_state("incomplete", "cleaning"), "cleaning")
        with self.assertRaises(ContractError):
            next_state("closed", "cleaning")
        with self.assertRaises(ContractError):
            next_state("ready", "provisioning")


if __name__ == "__main__":
    unittest.main()
