"""Assess normalized GCP project, billing, budget and control closure evidence.

This module has no cloud client. A separately authenticated read-only adapter
must collect the complete account/project/control inventories and retain the
response fingerprints represented here before a closure receipt can pass.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re

from independent_auditor import (
    AuditAssessment,
    AuditReportError,
    manifest_fingerprint,
)
from gcp_token_identity import PRINCIPAL_EMAIL
from run_contract import API_SERVICE, ContractError, validate_manifest
from run_ledger import LedgerSnapshot, RunLedger


CLOSURE_SCHEMA = "portfolio.gcp-closure.v2"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
AUDITOR_ID = re.compile(r"^google-user-[A-Za-z0-9._~-]{1,255}$")
BILLING_ACCOUNT = re.compile(
    r"^billingAccounts/[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$"
)
BUDGET_NAME = re.compile(
    r"^billingAccounts/[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}/budgets/[A-Za-z0-9_-]+$"
)
SURFACES = frozenset(
    {
        "resourcemanager.project",
        "serviceusage.enabled_services",
        "run_project.service_accounts",
        "billing.project_info",
        "billing.budgets",
        "run.jobs",
        "scheduler.jobs",
        "iam.service_accounts",
    }
)
CONTROL_SURFACE = {
    "gcp.run.job": "run.jobs",
    "gcp.scheduler.job": "scheduler.jobs",
    "gcp.iam.service_account": "iam.service_accounts",
}
REPORT_FIELDS = {
    "schema", "run_id", "project_id", "recovery_project_id",
    "manifest_sha256", "observed_at", "auditor_id", "auditor_email",
    "coverage", "project",
    "project_snapshot",
    "billing", "budget_scope", "budget_observed_at", "budgets",
    "controls_observed_at", "controls",
}


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuditReportError(
            "closure evidence must be finite canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise AuditReportError(f"{label} must be a SHA-256 hex digest")
    return value


def _time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise AuditReportError(f"{label} must be a timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditReportError(f"{label} must be ISO-8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise AuditReportError(f"{label} must include a timezone")
    return result


def _coverage(report: dict) -> tuple[bool, dict]:
    coverage = report["coverage"]
    if not isinstance(coverage, dict) or set(coverage) - SURFACES:
        raise AuditReportError("closure coverage contains unsupported surfaces")
    complete = set(coverage) == SURFACES
    normalized = {}
    for surface, entry in coverage.items():
        if not isinstance(entry, dict) or set(entry) != {
            "complete", "pages", "resource_count", "response_sha256"
        }:
            raise AuditReportError(f"closure coverage for {surface} is malformed")
        pages, count = entry["pages"], entry["resource_count"]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (pages, count)):
            raise AuditReportError(f"closure counts for {surface} are invalid")
        if not isinstance(entry["complete"], bool):
            raise AuditReportError(f"closure completeness for {surface} is invalid")
        _fingerprint(entry["response_sha256"], f"{surface} response fingerprint")
        if pages < 1 or entry["complete"] is not True:
            complete = False
        normalized[surface] = dict(entry)
    return complete, normalized


def assess_closure(manifest: object, report: object) -> AuditAssessment:
    """Return a ledger-ready assessment from independently collected evidence."""
    try:
        run = validate_manifest(manifest)
    except ContractError as exc:
        raise AuditReportError(f"run manifest violates the contract: {exc}") from exc
    if not isinstance(report, dict) or set(report) != REPORT_FIELDS:
        raise AuditReportError("closure report fields are incomplete or unsupported")
    if report["schema"] != CLOSURE_SCHEMA:
        raise AuditReportError("closure report schema is unsupported")
    if (report["run_id"] != run["run_id"]
            or report["project_id"] != run["scope"]["project_id"]
            or report["recovery_project_id"] != run["scope"]["recovery_project_id"]):
        raise AuditReportError(
            "closure report must match the exact run and project scope"
        )
    if report["manifest_sha256"] != manifest_fingerprint(run):
        raise AuditReportError(
            "closure report was collected for a different manifest snapshot"
        )
    auditor_id = report["auditor_id"]
    if not isinstance(auditor_id, str) or AUDITOR_ID.fullmatch(auditor_id) is None:
        raise AuditReportError("closure auditor_id must be a verified Google user subject")
    auditor_email = report["auditor_email"]
    if (not isinstance(auditor_email, str)
            or PRINCIPAL_EMAIL.fullmatch(auditor_email) is None):
        raise AuditReportError("closure auditor_email must be a verified user email")
    cleanup_principal = run["cleanup"]["service_account"]
    scheduler_principal = (
        f"{run['run_id']}-sched@{run['scope']['recovery_project_id']}"
        ".iam.gserviceaccount.com"
    )
    if auditor_email in {cleanup_principal, scheduler_principal}:
        raise AuditReportError(
            "closure auditor must be distinct from reaper and scheduler identities"
        )
    observed_text = report["observed_at"]
    observed_at = _time(observed_text, "closure observed_at")

    complete, coverage = _coverage(report)
    project = report["project"]
    if not isinstance(project, dict) or set(project) != {
        "exists", "observed_at", "response_sha256"
    }:
        raise AuditReportError("project observation is malformed")
    if not isinstance(project["exists"], bool):
        raise AuditReportError("project.exists must be boolean")
    _fingerprint(project["response_sha256"], "project response fingerprint")
    project_observed = _time(project["observed_at"], "project observed_at")
    if ("resourcemanager.project" in coverage
            and coverage["resourcemanager.project"]["resource_count"]
            != int(project["exists"])):
        raise AuditReportError("project coverage count contradicts its exact lookup")

    project_snapshot = report["project_snapshot"]
    if not isinstance(project_snapshot, dict) or set(project_snapshot) != {
        "project_id", "project_number", "observed_at", "enabled_services",
        "service_accounts",
    }:
        raise AuditReportError("pre-deletion run-project inventory is malformed")
    if project_snapshot["project_id"] != run["scope"]["project_id"]:
        raise AuditReportError(
            "pre-deletion inventory belongs to a different run project"
        )
    project_number = project_snapshot["project_number"]
    if (not isinstance(project_number, str) or not project_number.isdigit()
            or project_number.startswith("0")):
        raise AuditReportError(
            "project snapshot requires the exact numeric project number"
        )
    project_snapshot_observed = _time(
        project_snapshot["observed_at"], "project snapshot observed_at"
    )
    enabled_services = project_snapshot["enabled_services"]
    if (not isinstance(enabled_services, list)
            or any(not isinstance(service, str) or not API_SERVICE.fullmatch(service)
                   for service in enabled_services)
            or len(set(enabled_services)) != len(enabled_services)
            or enabled_services != sorted(enabled_services)):
        raise AuditReportError("enabled service inventory is malformed")
    service_accounts = project_snapshot["service_accounts"]
    expected_sa_prefix = (
        f"projects/{run['scope']['project_id']}/serviceAccounts/"
    )
    if (not isinstance(service_accounts, list)
            or any(not isinstance(name, str)
                   or not name.startswith(expected_sa_prefix)
                   or not name[len(expected_sa_prefix):]
                   or "/" in name[len(expected_sa_prefix):]
                   for name in service_accounts)
            or len(set(service_accounts)) != len(service_accounts)
            or service_accounts != sorted(service_accounts)):
        raise AuditReportError("run-project service account inventory is malformed")
    if ("serviceusage.enabled_services" in coverage
            and coverage["serviceusage.enabled_services"]["resource_count"]
            != len(enabled_services)):
        raise AuditReportError(
            "enabled API coverage count does not match its inventory"
        )
    if ("run_project.service_accounts" in coverage
            and coverage["run_project.service_accounts"]["resource_count"]
            != len(service_accounts)):
        raise AuditReportError(
            "service-account coverage count does not match its inventory"
        )
    if project_snapshot_observed >= project_observed:
        raise AuditReportError("run-project inventory must precede project absence")

    billing = report["billing"]
    if not isinstance(billing, dict) or set(billing) != {"before", "after"}:
        raise AuditReportError("billing observations are malformed")
    before, after = billing["before"], billing["after"]
    observation_fields = {
        "project_id", "billing_enabled", "billing_account_id", "observed_at",
        "response_sha256",
    }
    if (not isinstance(before, dict) or set(before) != observation_fields
            or not isinstance(after, dict) or set(after) != observation_fields):
        raise AuditReportError("billing before/after observations are malformed")
    if any(not isinstance(item["billing_enabled"], bool) for item in (before, after)):
        raise AuditReportError("billing_enabled observations must be boolean")
    if any(
        item["project_id"] != run["scope"]["project_id"]
        for item in (before, after)
    ):
        raise AuditReportError("billing observations belong to a different run project")
    expected_account = run["finance"]["billing_account_id"]
    if (before["billing_account_id"] != expected_account
            or not BILLING_ACCOUNT.fullmatch(expected_account)):
        raise AuditReportError("pre-unlink billing account does not match the manifest")
    if after["billing_account_id"] not in (None, "") and (
        not isinstance(after["billing_account_id"], str)
        or not BILLING_ACCOUNT.fullmatch(after["billing_account_id"])
    ):
        raise AuditReportError("post-unlink billing account name is malformed")
    before_observed = _time(before["observed_at"], "pre-unlink billing observed_at")
    after_observed = _time(after["observed_at"], "post-unlink billing observed_at")
    _fingerprint(before["response_sha256"], "pre-unlink billing fingerprint")
    _fingerprint(after["response_sha256"], "post-unlink billing fingerprint")
    if before_observed >= after_observed:
        raise AuditReportError("billing unlink observations must be chronological")
    if after_observed >= project_observed:
        raise AuditReportError("billing unlink must precede project deletion")
    if ("billing.project_info" in coverage
            and coverage["billing.project_info"]["resource_count"] != 2):
        raise AuditReportError("billing coverage must contain both unlink observations")

    if report["budget_scope"] != f"projects/{run['scope']['project_id']}":
        raise AuditReportError(
            "budget inventory must be scoped to the exact run project"
        )
    budgets = report["budgets"]
    budget_observed = _time(report["budget_observed_at"], "budget observed_at")
    if not isinstance(budgets, list):
        raise AuditReportError("budgets must be a complete inventory list")
    budget_ids = set()
    normalized_budgets = []
    for index, budget in enumerate(budgets):
        budget_id = budget
        if not isinstance(budget_id, str) or BUDGET_NAME.fullmatch(budget_id) is None:
            raise AuditReportError(f"budget {index} has a malformed resource name")
        if not budget_id.startswith(expected_account + "/budgets/"):
            raise AuditReportError(
                f"budget {index} is outside the manifest billing account"
            )
        if budget_id in budget_ids:
            raise AuditReportError("budget inventory contains a duplicate ID")
        budget_ids.add(budget_id)
        normalized_budgets.append(budget_id)
    budget_for_run_project = bool(normalized_budgets)
    if ("billing.budgets" in coverage
            and coverage["billing.budgets"]["resource_count"] != len(budgets)):
        raise AuditReportError(
            "budget coverage count does not match the normalized inventory"
        )

    controls = report["controls"]
    controls_observed = _time(report["controls_observed_at"], "controls observed_at")
    if not isinstance(controls, list):
        raise AuditReportError("controls must be an inventory list")
    control_ids = set()
    normalized_controls = []
    run_control_present = False
    for index, control in enumerate(controls):
        if not isinstance(control, dict) or set(control) != {"type", "id"}:
            raise AuditReportError(f"control {index} is malformed")
        resource_type, resource_id = control["type"], control["id"]
        if resource_type not in CONTROL_SURFACE or not isinstance(resource_id, str):
            raise AuditReportError(f"control {index} has an unsupported type or ID")
        recovery_project = run["scope"]["recovery_project_id"]
        prefix = f"projects/{recovery_project}/"
        expected_prefix = (
            f"{prefix}locations/europe-west2/jobs/"
            if resource_type in {"gcp.run.job", "gcp.scheduler.job"}
            else f"{prefix}serviceAccounts/"
        )
        if not resource_id.startswith(expected_prefix):
            raise AuditReportError(
                f"control {index} crosses the recovery project scope"
            )
        if resource_id in control_ids:
            raise AuditReportError("control inventory contains a duplicate ID")
        control_ids.add(resource_id)
        if run["run_id"] in resource_id:
            run_control_present = True
        normalized_controls.append({"type": resource_type, "id": resource_id})
    for resource_type, surface in CONTROL_SURFACE.items():
        count = sum(item["type"] == resource_type for item in normalized_controls)
        if surface in coverage and coverage[surface]["resource_count"] != count:
            raise AuditReportError(
                f"{surface} count does not match normalized controls"
            )
    latest_observation = max(
        project_observed, after_observed, budget_observed, controls_observed
    )
    if latest_observation > observed_at:
        raise AuditReportError("closure observations cannot postdate the report")

    manifest_controls_deleted = all(
        item["status"] == "deleted" for item in run["controls"]
    )
    workload_receipt = run.get("independent_audit")
    workload_passed = (
        isinstance(workload_receipt, dict)
        and workload_receipt.get("phase") == "workload"
        and workload_receipt.get("result") == "passed"
        and workload_receipt.get("complete") is True
    )
    inventory_complete = complete and workload_passed
    checks = {
        "resource_inventory_complete": inventory_complete,
        "resources_absent": workload_passed and all(
            item["status"] == "deleted" for item in run["resources"]
        ),
        "project_absent": complete and project["exists"] is False,
        "project_services_reconciled": complete and enabled_services
            == run["scope"]["enabled_api_services"],
        "billing_unlinked": complete and before["billing_enabled"] is True
            and after["billing_enabled"] is False
            and after["billing_account_id"] in (None, ""),
        "budget_reconciled": complete and not budget_for_run_project,
        "recovery_controls_absent": complete and manifest_controls_deleted
            and not run_control_present and not (control_ids & {
                item["id"] for item in run["controls"]
            }),
    }
    normalized_controls.sort(key=lambda item: (item["type"], item["id"]))
    normalized_budgets.sort()
    inventory = {
        "run_project": {
            "project_number": project_number,
            "enabled_services": enabled_services,
            "service_accounts": service_accounts,
        },
        "budgets": normalized_budgets,
        "controls": normalized_controls,
    }
    inventory_sha256 = _digest(inventory)
    evidence_sha256 = _digest({
        "schema": report["schema"], "run_id": report["run_id"],
        "project_id": report["project_id"],
        "recovery_project_id": report["recovery_project_id"],
        "manifest_sha256": report["manifest_sha256"], "observed_at": observed_text,
        "auditor_id": auditor_id, "auditor_email": auditor_email,
        "coverage": coverage, "project": project,
        "project_snapshot": project_snapshot,
        "billing": billing, "budget_observed_at": report["budget_observed_at"],
        "controls_observed_at": report["controls_observed_at"], "inventory": inventory,
    })
    if not complete:
        result = "incomplete"
    elif all(checks.values()):
        result = "passed"
    else:
        result = "failed"
    return AuditAssessment(
        result=result, complete=complete, checks=checks,
        inventory_sha256=inventory_sha256, evidence_sha256=evidence_sha256,
        observed_at=observed_at, auditor_id=auditor_id,
    )


def record_closure_audit(
    ledger: RunLedger,
    run_id: str,
    report: object,
    *,
    expected_generation: int,
    lease_owner: str,
    now: datetime | None = None,
) -> tuple[LedgerSnapshot, AuditAssessment]:
    """Assess external closure evidence, then append its minimized receipt."""
    assessment = assess_closure(ledger.read(run_id).manifest, report)
    snapshot = ledger.record_independent_audit(
        run_id, expected_generation=expected_generation, lease_owner=lease_owner,
        auditor_id=assessment.auditor_id, phase="closure", result=assessment.result,
        complete=assessment.complete, inventory_sha256=assessment.inventory_sha256,
        evidence_sha256=assessment.evidence_sha256, checks=assessment.checks,
        observed_at=assessment.observed_at, now=now,
    )
    return snapshot, assessment
