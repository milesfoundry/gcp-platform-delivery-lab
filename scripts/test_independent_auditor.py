"""Fail-closed tests for normalized independent GCP inventory evidence."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from independent_auditor import (
    AUDIT_SCHEMA,
    WORKLOAD_SURFACES,
    AuditReportError,
    assess_workload_inventory,
    manifest_fingerprint,
    record_workload_audit,
)
from run_ledger import RunLedger
from test_run_contract import PROJECT, RUN_ID, intent, resource
from test_run_ledger import FakeObjectStore


def report(
    *,
    coverage=None,
    resources=None,
    run=None,
    observed_at="2026-10-19T01:00:00Z",
):
    manifest = run or intent()
    if coverage is None:
        coverage = {
            surface: {
                "complete": True,
                "pages": 1,
                "resource_count": 0,
                "response_sha256": f"{index:064x}",
            }
            for index, surface in enumerate(sorted(WORKLOAD_SURFACES), start=1)
        }
    return {
        "schema": AUDIT_SCHEMA,
        "run_id": manifest["run_id"],
        "project_id": manifest["scope"]["project_id"],
        "manifest_sha256": manifest_fingerprint(manifest),
        "observed_at": observed_at,
        "auditor_id": "independent-reader",
        "coverage": coverage,
        "resources": [] if resources is None else resources,
    }


class IndependentAuditorTests(unittest.TestCase):
    def test_complete_empty_inventory_passes_with_stable_fingerprints(self):
        manifest = intent()
        result = assess_workload_inventory(manifest, report())
        self.assertEqual(result.result, "passed")
        self.assertTrue(result.complete)
        self.assertEqual(
            result.checks,
            {"resource_inventory_complete": True, "resources_absent": True},
        )
        self.assertEqual(
            result.inventory_sha256,
            assess_workload_inventory(manifest, report()).inventory_sha256,
        )

    def test_missing_or_failed_surface_is_incomplete_not_empty(self):
        incomplete = report(coverage={})
        result = assess_workload_inventory(intent(), incomplete)
        self.assertEqual(result.result, "incomplete")
        self.assertFalse(result.complete)
        self.assertFalse(result.checks["resources_absent"])

        coverage = report()["coverage"]
        coverage["compute.disks"]["complete"] = False
        result = assess_workload_inventory(intent(), report(coverage=coverage))
        self.assertEqual(result.result, "incomplete")
        self.assertFalse(result.checks["resource_inventory_complete"])

    def test_any_observed_workload_or_derived_resource_fails_a_complete_audit(self):
        evidence = report()
        evidence["resources"] = [
            {
                "surface": "compute.disks",
                "project_id": PROJECT,
                "id": f"projects/{PROJECT}/zones/europe-west2-a/disks/orphan-disk",
            }
        ]
        evidence["coverage"]["compute.disks"]["resource_count"] = 1
        result = assess_workload_inventory(intent(), evidence)
        self.assertEqual(result.result, "failed")
        self.assertTrue(result.complete)
        self.assertFalse(result.checks["resources_absent"])

    def test_manifest_must_already_record_all_deletes_as_terminal(self):
        manifest = intent()
        manifest["state"] = "auditing"
        manifest["resources"] = [
            resource("gcp.compute.network", RUN_ID + "-network", status="present")
        ]
        result = assess_workload_inventory(manifest, report(run=manifest))
        self.assertEqual(result.result, "failed")
        self.assertFalse(result.checks["resources_absent"])

    def test_report_scope_and_coverage_contract_fail_closed(self):
        evidence = report()
        evidence["project_id"] = "different-project"
        with self.assertRaises(AuditReportError):
            assess_workload_inventory(intent(), evidence)

        different_manifest = intent()
        different_manifest["source"]["revision"] = "c" * 40
        with self.assertRaises(AuditReportError):
            assess_workload_inventory(different_manifest, report())

        evidence = report()
        evidence["coverage"]["compute.new_surface"] = {
            "complete": True,
            "pages": 1,
            "resource_count": 0,
            "response_sha256": "a" * 64,
        }
        with self.assertRaises(AuditReportError):
            assess_workload_inventory(intent(), evidence)

        evidence = report()
        evidence["coverage"]["compute.networks"]["resource_count"] = 1
        with self.assertRaises(AuditReportError):
            assess_workload_inventory(intent(), evidence)

    def test_duplicate_resource_ids_are_rejected(self):
        evidence = report()
        observed = {
            "project_id": PROJECT,
            "id": f"projects/{PROJECT}/global/networks/{RUN_ID}-network",
        }
        evidence["resources"] = [
            {"surface": "compute.networks", **observed},
            {"surface": "compute.networks", **observed},
        ]
        evidence["coverage"]["compute.networks"]["resource_count"] = 2
        with self.assertRaises(AuditReportError):
            assess_workload_inventory(intent(), evidence)

    def test_assessment_is_recorded_as_a_generation_bound_workload_receipt(self):
        now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
        ledger = RunLedger(FakeObjectStore(), clock=lambda: now)
        snapshot = ledger.create(intent())
        snapshot = ledger.acquire_lease(
            RUN_ID,
            expected_generation=snapshot.generation,
            owner="launcher",
            now=now,
        )
        for index, state in enumerate(
            [
                "prepared", "project_create_requested", "project_created",
                "billing_link_requested", "project_linked", "api_enablement",
                "provisioning", "ready", "teardown_requested", "cleaning", "auditing",
            ],
            start=1,
        ):
            candidate = deepcopy(snapshot.manifest)
            candidate["state"] = state
            if state == "prepared":
                for control in candidate["controls"]:
                    control["status"] = "present"
            snapshot = ledger.replace(
                RUN_ID,
                expected_generation=snapshot.generation,
                lease_owner="launcher",
                manifest=candidate,
                now=now + timedelta(seconds=index),
            )
        snapshot = ledger.release_lease(
            RUN_ID,
            expected_generation=snapshot.generation,
            owner="launcher",
            now=now + timedelta(seconds=20),
        )
        snapshot = ledger.acquire_lease(
            RUN_ID,
            expected_generation=snapshot.generation,
            owner="auditor:independent-reader",
            now=now + timedelta(seconds=21),
        )
        evidence = report(
            run=snapshot.manifest,
            observed_at="2026-10-18T12:00:22Z",
        )
        recorded, assessment = record_workload_audit(
            ledger,
            RUN_ID,
            evidence,
            expected_generation=snapshot.generation,
            lease_owner="auditor:independent-reader",
            now=now + timedelta(seconds=23),
        )
        self.assertEqual(assessment.result, "passed")
        self.assertEqual(
            recorded.manifest["independent_audit"]["phase"], "workload"
        )
        self.assertEqual(
            recorded.manifest["audit_receipts"][0]["manifest_generation"],
            snapshot.generation,
        )
        self.assertEqual(
            recorded.manifest["audit_receipts"][0]["observed_at"],
            "2026-10-18T12:00:22Z",
        )
        self.assertEqual(
            recorded.metadata["events"][-1]["at"], "2026-10-18T12:00:23Z"
        )


if __name__ == "__main__":
    unittest.main()
