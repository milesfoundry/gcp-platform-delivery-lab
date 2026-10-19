"""Fail-closed tests for normalized GCP project and control closure evidence."""

from copy import deepcopy
import unittest

from closure_auditor import (
    CLOSURE_SCHEMA,
    SURFACES,
    AuditReportError,
    assess_closure,
)
from independent_auditor import manifest_fingerprint
from run_contract import _audit_summary
from test_run_contract import attach_control_receipt, audit_receipt, intent, retirement_operations


def closure_ready_manifest():
    manifest = intent()
    manifest["state"] = "controls_reclaiming"
    manifest["operations"] = retirement_operations()
    for control in manifest["controls"]:
        control["status"] = "present"
    attach_control_receipt(manifest)
    for control in manifest["controls"]:
        control["status"] = "deleted"
    receipt = audit_receipt(1, "workload")
    manifest["audit_receipts"] = [receipt]
    manifest["independent_audit"] = _audit_summary(receipt)
    return manifest


def report(*, manifest=None, coverage=None, budgets=None, controls=None):
    run = manifest or closure_ready_manifest()
    service_accounts = [
        f"projects/{run['scope']['project_id']}/serviceAccounts/"
        f"service-123456789@gcp-sa-container.iam.gserviceaccount.com"
    ]
    if coverage is None:
        counts = {
            "resourcemanager.project": 0,
            "serviceusage.enabled_services": len(
                run["scope"]["enabled_api_services"]
            ),
            "run_project.service_accounts": len(service_accounts),
            "billing.project_info": 2,
        }
        coverage = {
            surface: {
                "complete": True,
                "pages": 2 if surface == "billing.project_info" else 1,
                "resource_count": counts.get(surface, 0),
                "response_sha256": f"{index:064x}",
            }
            for index, surface in enumerate(sorted(SURFACES), start=1)
        }
    return {
        "schema": CLOSURE_SCHEMA,
        "run_id": run["run_id"],
        "project_id": run["scope"]["project_id"],
        "recovery_project_id": run["scope"]["recovery_project_id"],
        "manifest_sha256": manifest_fingerprint(run),
        "observed_at": "2026-10-19T01:30:00Z",
        "auditor_id": "google-user-12345678901234567890",
        "auditor_email": "independent-reader@example.com",
        "coverage": coverage,
        "project_snapshot": {
            "project_id": run["scope"]["project_id"],
            "project_number": "123456789",
            "observed_at": "2026-10-19T01:20:00Z",
            "enabled_services": run["scope"]["enabled_api_services"],
            "service_accounts": service_accounts,
        },
        "project": {
            "exists": False,
            "observed_at": "2026-10-19T01:25:00Z",
            "response_sha256": "a" * 64,
        },
        "billing": {
            "before": {
                "project_id": run["scope"]["project_id"],
                "billing_enabled": True,
                "billing_account_id": run["finance"]["billing_account_id"],
                "observed_at": "2026-10-19T01:10:00Z",
                "response_sha256": "b" * 64,
            },
            "after": {
                "project_id": run["scope"]["project_id"],
                "billing_enabled": False,
                "billing_account_id": None,
                "observed_at": "2026-10-19T01:15:00Z",
                "response_sha256": "c" * 64,
            },
        },
        "budget_scope": f"projects/{run['scope']['project_id']}",
        "budget_observed_at": "2026-10-19T01:26:00Z",
        "budgets": [] if budgets is None else budgets,
        "controls_observed_at": "2026-10-19T01:27:00Z",
        "controls": [] if controls is None else controls,
    }


class ClosureAuditorTests(unittest.TestCase):
    def test_complete_closure_evidence_passes(self):
        result = assess_closure(closure_ready_manifest(), report())
        self.assertEqual(result.result, "passed")
        self.assertEqual(
            result.checks,
            {
                "resource_inventory_complete": True,
                "resources_absent": True,
                "project_absent": True,
                "project_services_reconciled": True,
                "billing_unlinked": True,
                "budget_reconciled": True,
                "recovery_controls_absent": True,
            },
        )

    def test_partial_coverage_is_incomplete(self):
        coverage = report()["coverage"]
        del coverage["billing.budgets"]
        result = assess_closure(closure_ready_manifest(), report(coverage=coverage))
        self.assertEqual(result.result, "incomplete")
        self.assertFalse(result.checks["budget_reconciled"])

    def test_missing_passed_workload_receipt_cannot_prove_resource_absence(self):
        run = intent()
        evidence = report(manifest=run)
        result = assess_closure(run, evidence)
        self.assertEqual(result.result, "failed")
        self.assertFalse(result.checks["resource_inventory_complete"])
        self.assertFalse(result.checks["resources_absent"])

    def test_project_budget_and_control_residue_fail(self):
        run = closure_ready_manifest()
        project_budget = run["finance"]["billing_account_id"] + "/budgets/run-budget"
        evidence = report(manifest=run, budgets=[project_budget])
        evidence["coverage"]["billing.budgets"]["resource_count"] = 1
        result = assess_closure(run, evidence)
        self.assertEqual(result.result, "failed")
        self.assertFalse(result.checks["budget_reconciled"])

        control = {
            "type": "gcp.run.job",
            "id": run["controls"][0]["id"],
        }
        evidence = report(manifest=run, controls=[control])
        evidence["coverage"]["run.jobs"]["resource_count"] = 1
        result = assess_closure(run, evidence)
        self.assertEqual(result.result, "failed")
        self.assertFalse(result.checks["recovery_controls_absent"])

    def test_unlinked_billing_requires_expected_account_and_post_state(self):
        run = closure_ready_manifest()
        evidence = report(manifest=run)
        evidence["billing"]["before"]["billing_account_id"] = (
            "billingAccounts/AAAAAA-BBBBBB-CCCCCC"
        )
        with self.assertRaises(AuditReportError):
            assess_closure(run, evidence)

        evidence = report(manifest=run)
        evidence["billing"]["before"]["project_id"] = "other-project"
        with self.assertRaises(AuditReportError):
            assess_closure(run, evidence)

        evidence = report(manifest=run)
        evidence["billing"]["after"]["billing_enabled"] = True
        result = assess_closure(run, evidence)
        self.assertEqual(result.result, "failed")
        self.assertFalse(result.checks["billing_unlinked"])

    def test_budget_scope_is_exact_and_unrelated_budget_is_not_run_owned(self):
        run = closure_ready_manifest()
        evidence = report(manifest=run)
        evidence["budget_scope"] = "projects/some-other-project"
        with self.assertRaises(AuditReportError):
            assess_closure(run, evidence)

    def test_auditor_identity_cannot_match_run_reaper_or_scheduler(self):
        run = closure_ready_manifest()
        evidence = report(manifest=run)
        evidence["auditor_email"] = run["cleanup"]["service_account"]
        with self.assertRaises(AuditReportError):
            assess_closure(run, evidence)

    def test_enabled_api_inventory_must_match_the_manifest(self):
        run = closure_ready_manifest()
        evidence = report(manifest=run)
        evidence["project_snapshot"]["enabled_services"] = [
            *run["scope"]["enabled_api_services"],
            "unexpected.googleapis.com",
        ]
        evidence["project_snapshot"]["enabled_services"].sort()
        evidence["coverage"]["serviceusage.enabled_services"]["resource_count"] += 1
        result = assess_closure(run, evidence)
        self.assertEqual(result.result, "failed")
        self.assertFalse(result.checks["project_services_reconciled"])

    def test_billing_observations_must_be_ordered_and_precede_report(self):
        run = closure_ready_manifest()
        evidence = report(manifest=run)
        evidence["billing"]["after"]["observed_at"] = "2026-10-19T01:09:00Z"
        with self.assertRaises(AuditReportError):
            assess_closure(run, evidence)

        evidence = report(manifest=run)
        evidence["billing"]["after"]["observed_at"] = "2026-10-19T01:26:00Z"
        with self.assertRaises(AuditReportError):
            assess_closure(run, evidence)

        evidence = report(manifest=run)
        evidence["controls_observed_at"] = "2026-10-19T01:31:00Z"
        with self.assertRaises(AuditReportError):
            assess_closure(run, evidence)

    def test_manifest_binding_and_duplicate_inventory_fail_closed(self):
        run = closure_ready_manifest()
        evidence = report(manifest=run)
        changed = deepcopy(run)
        changed["source"]["revision"] = "d" * 40
        with self.assertRaises(AuditReportError):
            assess_closure(changed, evidence)

        budget_id = run["finance"]["billing_account_id"] + "/budgets/shared"
        duplicate = budget_id
        evidence = report(manifest=run, budgets=[duplicate, duplicate])
        evidence["coverage"]["billing.budgets"]["resource_count"] = 2
        with self.assertRaises(AuditReportError):
            assess_closure(run, evidence)


if __name__ == "__main__":
    unittest.main()
