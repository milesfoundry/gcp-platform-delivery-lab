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
    register_discovered_workload_resources,
    record_workload_audit,
)
from run_contract import cleanup_plan
from run_ledger import RunLedger
from test_run_contract import PROJECT, RUN_ID, attach_control_receipt, intent, resource
from test_run_ledger import (
    FakeObjectStore,
    advance_ledger_to_provisioning,
    current_intent,
)


def report(
    *,
    coverage=None,
    resources=None,
    run=None,
    observed_at="2026-10-19T01:00:00Z",
):
    manifest = run or intent()
    if (
        manifest["state"] not in {"control_bootstrapping", "incomplete"}
        and not manifest.get("recovery_control_receipts")
    ):
        for control in manifest["controls"]:
            if control["status"] == "expected":
                control["status"] = "present"
        attach_control_receipt(manifest)
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
        "auditor_id": "google-user-12345678901234567890",
        "auditor_email": "independent-reader@example.com",
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

    def test_authenticated_run_named_discovery_is_registered_under_auditor_lease(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        manifest = current_intent()
        store = FakeObjectStore()
        ledger = RunLedger(store, clock=lambda: now)
        snapshot = ledger.create(manifest)
        snapshot = ledger.acquire_lease(
            RUN_ID, expected_generation=snapshot.generation, owner="launcher", now=now
        )
        snapshot = advance_ledger_to_provisioning(ledger, snapshot, now)
        for index, state in enumerate(
            ["ready", "teardown_requested", "cleaning"], start=10
        ):
            candidate = deepcopy(snapshot.manifest)
            candidate["state"] = state
            snapshot = ledger.replace(
                RUN_ID,
                expected_generation=snapshot.generation,
                lease_owner="launcher",
                manifest=candidate,
                now=now + timedelta(seconds=index + 1),
            )
        snapshot = ledger.release_lease(
            RUN_ID,
            expected_generation=snapshot.generation,
            owner="launcher",
            now=now + timedelta(seconds=20),
        )
        found = {
            "surface": "compute.networks",
            "project_id": PROJECT,
            "id": f"projects/{PROJECT}/global/networks/{RUN_ID}-recovered",
        }
        unowned = {
            "surface": "compute.networks",
            "project_id": PROJECT,
            "id": f"projects/{PROJECT}/global/networks/shared-network",
        }
        unsupported = {
            "surface": "compute.disks",
            "project_id": PROJECT,
            "id": f"projects/{PROJECT}/zones/europe-west2-a/disks/{RUN_ID}-disk",
        }
        findings = [found, unowned, unsupported]

        class Collector:
            def collect(self, source_manifest, *, observed_at, expected_auditor_email):
                evidence = report(
                    run=source_manifest,
                    resources=findings,
                    observed_at=observed_at,
                )
                evidence["auditor_email"] = expected_auditor_email
                evidence["coverage"]["compute.networks"]["resource_count"] = 2
                evidence["coverage"]["compute.disks"]["resource_count"] = 1
                return evidence

        class IncompleteCollector:
            def collect(self, source_manifest, *, observed_at, expected_auditor_email):
                evidence = report(
                    run=source_manifest,
                    coverage={},
                    observed_at=observed_at,
                )
                evidence["auditor_email"] = expected_auditor_email
                return evidence

        generation_before_incomplete = ledger.read(RUN_ID).generation
        with self.assertRaisesRegex(AuditReportError, "incomplete inventory"):
            register_discovered_workload_resources(
                ledger,
                RUN_ID,
                IncompleteCollector(),
                expected_auditor_email="independent-reader@example.com",
                now=now + timedelta(seconds=29),
            )
        self.assertEqual(ledger.read(RUN_ID).generation, generation_before_incomplete)
        self.assertIsNone(ledger.read(RUN_ID).metadata["lease"]["owner"])

        result = register_discovered_workload_resources(
            ledger,
            RUN_ID,
            Collector(),
            expected_auditor_email="independent-reader@example.com",
            now=now + timedelta(seconds=30),
        )
        self.assertEqual(result.registered_count, 1)
        self.assertEqual(result.unregistered_count, 2)
        self.assertEqual(result.snapshot.manifest["state"], "cleaning")
        self.assertEqual(result.snapshot.manifest["resources"], [{
            "type": "gcp.compute.network",
            "id": found["id"],
            "ownership_proof": "run_name",
            "status": "present",
            "delete_attempts": 0,
        }])
        self.assertEqual(
            result.snapshot.manifest["discovery_receipts"][-1]["auditor_id"],
            "google-user-12345678901234567890",
        )
        self.assertEqual(cleanup_plan(result.snapshot.manifest), result.snapshot.manifest["resources"])
        self.assertIsNone(result.snapshot.metadata["lease"]["owner"])

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

    def test_gcs_inventory_ids_require_canonical_percent_encoding(self):
        for resource_id in (
            "gs://bucket-a/active/report%2f.json",
            "gs://bucket-a/active/report%ZZ.json",
            "projects/other-lab-456/buckets/bucket-a/objects/report%3F.json@7",
            f"projects/{PROJECT}/buckets/bucket-a/objects/report%3F.json@live",
        ):
            evidence = report()
            evidence["resources"] = [{
                "surface": "storage.object_versions",
                "project_id": PROJECT,
                "id": resource_id,
            }]
            evidence["coverage"]["storage.object_versions"]["resource_count"] = 1
            with self.subTest(resource_id=resource_id):
                with self.assertRaises(AuditReportError):
                    assess_workload_inventory(intent(), evidence)

    def test_auditor_identity_cannot_match_run_reaper_or_scheduler(self):
        manifest = intent()
        evidence = report(run=manifest)
        evidence["auditor_email"] = manifest["cleanup"]["service_account"]
        with self.assertRaises(AuditReportError):
            assess_workload_inventory(manifest, evidence)

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
        snapshot = advance_ledger_to_provisioning(ledger, snapshot, now)
        for index, state in enumerate(
            ["ready", "teardown_requested", "cleaning", "auditing"],
            start=10,
        ):
            candidate = deepcopy(snapshot.manifest)
            candidate["state"] = state
            snapshot = ledger.replace(
                RUN_ID,
                expected_generation=snapshot.generation,
                lease_owner="launcher",
                manifest=candidate,
                now=now + timedelta(seconds=index + 1),
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
            owner="auditor:google-user-12345678901234567890",
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
            lease_owner="auditor:google-user-12345678901234567890",
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
