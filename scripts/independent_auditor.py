"""Validate normalized read-only GCP inventory evidence for one run.

This module has no cloud client. A separately authenticated adapter must query
each required surface, fully paginate its results, and normalize resources into
the report format. Missing or partial coverage cannot produce a passing audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re

from run_contract import ContractError, validate_manifest
from run_ledger import LedgerSnapshot, RunLedger


AUDIT_SCHEMA = "portfolio.gcp-inventory.v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
AUDITOR_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:/._@+=,-]{0,1023}$")

# These surfaces follow the current resource/ownership matrix. The future GCP
# adapter must prove that every call covers every page and include a response
# fingerprint. Additional derived-resource surfaces require a reviewed change
# here; an adapter cannot silently widen its own completeness claim.
WORKLOAD_SURFACES = frozenset(
    {
        "compute.addresses",
        "compute.backend_services",
        "compute.disks",
        "compute.firewalls",
        "compute.forwarding_rules",
        "compute.health_checks",
        "compute.instances",
        "compute.instance_groups",
        "compute.networks",
        "compute.routes",
        "compute.router_nats",
        "compute.routers",
        "compute.subnetworks",
        "compute.target_instances",
        "compute.target_pools",
        "container.clusters",
        "container.node_pools",
        "gkehub.feature_memberships",
        "gkehub.features",
        "gkehub.memberships",
        "pubsub.subscriptions",
        "pubsub.topics",
        "storage.buckets",
        "storage.objects",
        "storage.object_versions",
    }
)

REPORT_FIELDS = {
    "schema", "run_id", "project_id", "manifest_sha256", "observed_at",
    "auditor_id", "coverage", "resources",
}
COVERAGE_FIELDS = {"complete", "pages", "resource_count", "response_sha256"}
RESOURCE_FIELDS = {"surface", "project_id", "id"}


class AuditReportError(ValueError):
    """A normalized audit report is malformed or crosses the requested scope."""


@dataclass(frozen=True)
class AuditAssessment:
    """Minimized result ready to append to the run ledger."""

    result: str
    complete: bool
    checks: dict
    inventory_sha256: str
    evidence_sha256: str
    observed_at: datetime
    auditor_id: str


def _canonical_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AuditReportError("audit evidence must be finite canonical JSON") from exc
    return encoded.encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def manifest_fingerprint(manifest: object) -> str:
    """Fingerprint the exact validated manifest snapshot used for discovery."""
    try:
        validated = validate_manifest(manifest)
    except ContractError as exc:
        raise AuditReportError(f"run manifest violates the contract: {exc}") from exc
    return _digest(validated)


def assess_workload_inventory(manifest: object, report: object) -> AuditAssessment:
    """Assess whether a complete external inventory proves workload absence.

    A complete inventory containing any workload or derived resource is a
    failed audit. Missing surfaces, partial pagination, or absent response
    fingerprints produce an incomplete audit. A pass also requires every
    manifest workload resource to have reached its terminal deleted status.
    """
    try:
        run = validate_manifest(manifest)
    except ContractError as exc:
        raise AuditReportError(f"run manifest violates the contract: {exc}") from exc
    if not isinstance(report, dict) or set(report) != REPORT_FIELDS:
        raise AuditReportError("audit report fields are incomplete or unsupported")
    if report["schema"] != AUDIT_SCHEMA:
        raise AuditReportError("audit report schema is unsupported")
    if (
        report["run_id"] != run["run_id"]
        or report["project_id"] != run["scope"]["project_id"]
    ):
        raise AuditReportError(
            "audit report must match the exact run and project scope"
        )
    expected_manifest_sha256 = manifest_fingerprint(run)
    if report["manifest_sha256"] != expected_manifest_sha256:
        raise AuditReportError(
            "audit report was collected for a different manifest snapshot"
        )
    auditor_id = report["auditor_id"]
    if not isinstance(auditor_id, str) or AUDITOR_ID.fullmatch(auditor_id) is None:
        raise AuditReportError("audit report auditor_id is unsupported")
    observed_at = report["observed_at"]
    if not isinstance(observed_at, str):
        raise AuditReportError("audit report observed_at must be a timestamp")
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditReportError("audit report observed_at must be ISO-8601") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise AuditReportError("audit report observed_at must include a timezone")

    coverage = report["coverage"]
    if not isinstance(coverage, dict):
        raise AuditReportError("audit report coverage must be an object")
    unknown_surfaces = set(coverage) - WORKLOAD_SURFACES
    if unknown_surfaces:
        raise AuditReportError(
            f"audit report contains unsupported surfaces: {sorted(unknown_surfaces)}"
        )
    complete = set(coverage) == WORKLOAD_SURFACES
    normalized_coverage = {}
    for surface, entry in coverage.items():
        if not isinstance(entry, dict) or set(entry) != COVERAGE_FIELDS:
            raise AuditReportError(f"coverage receipt for {surface} is malformed")
        if not isinstance(entry["complete"], bool):
            raise AuditReportError(f"coverage complete for {surface} must be boolean")
        pages = entry["pages"]
        count = entry["resource_count"]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (pages, count)):
            raise AuditReportError(
                f"coverage counts for {surface} must be non-negative integers"
            )
        fingerprint = entry["response_sha256"]
        if not isinstance(fingerprint, str) or SHA256.fullmatch(fingerprint) is None:
            raise AuditReportError(
                f"coverage response fingerprint for {surface} is invalid"
            )
        if pages < 1:
            complete = False
        if entry["complete"] is not True:
            complete = False
        normalized_coverage[surface] = {
            "complete": entry["complete"],
            "pages": pages,
            "resource_count": count,
            "response_sha256": fingerprint,
        }

    resources = report["resources"]
    if not isinstance(resources, list):
        raise AuditReportError("audit report resources must be a list")
    seen_ids = set()
    normalized_resources = []
    observed_by_surface = {surface: 0 for surface in WORKLOAD_SURFACES}
    for index, resource in enumerate(resources):
        if not isinstance(resource, dict) or set(resource) != RESOURCE_FIELDS:
            raise AuditReportError(f"audit resource {index} is malformed")
        surface, project_id, resource_id = (
            resource["surface"], resource["project_id"], resource["id"]
        )
        if surface not in WORKLOAD_SURFACES:
            raise AuditReportError(f"audit resource {index} has an unsupported surface")
        if project_id != run["scope"]["project_id"]:
            raise AuditReportError(
                f"audit resource {index} crosses the run project scope"
            )
        if (
            not isinstance(resource_id, str)
            or RESOURCE_ID.fullmatch(resource_id) is None
        ):
            raise AuditReportError(
                f"audit resource {index} has an invalid canonical ID"
            )
        if resource_id in seen_ids:
            raise AuditReportError("audit report contains a duplicate resource ID")
        seen_ids.add(resource_id)
        observed_by_surface[surface] += 1
        normalized_resources.append(
            {"surface": surface, "project_id": project_id, "id": resource_id}
        )
    if any(
        normalized_coverage.get(surface, {}).get("resource_count")
        != observed_by_surface[surface]
        for surface in normalized_coverage
    ):
        raise AuditReportError(
            "coverage resource counts do not match normalized inventory"
        )

    # Canonical order makes fingerprints stable across API page/order changes.
    normalized_resources.sort(key=lambda item: (item["surface"], item["id"]))
    inventory_sha256 = _digest(normalized_resources)
    evidence_sha256 = _digest(
        {
            "schema": report["schema"],
            "run_id": report["run_id"],
            "project_id": report["project_id"],
            "manifest_sha256": report["manifest_sha256"],
            "observed_at": observed_at,
            "auditor_id": auditor_id,
            "coverage": normalized_coverage,
            "resources": normalized_resources,
        }
    )
    resources_absent = complete and not normalized_resources and all(
        item["status"] == "deleted" for item in run["resources"]
    )
    if not complete:
        result = "incomplete"
    elif resources_absent:
        result = "passed"
    else:
        result = "failed"
    return AuditAssessment(
        result=result,
        complete=complete,
        checks={
            "resource_inventory_complete": complete,
            "resources_absent": resources_absent,
        },
        inventory_sha256=inventory_sha256,
        evidence_sha256=evidence_sha256,
        observed_at=observed,
        auditor_id=auditor_id,
    )


def record_workload_audit(
    ledger: RunLedger,
    run_id: str,
    report: object,
    *,
    expected_generation: int,
    lease_owner: str,
    now: datetime | None = None,
) -> tuple[LedgerSnapshot, AuditAssessment]:
    """Assess external evidence, then append its minimized receipt to the ledger."""
    assessment = assess_workload_inventory(ledger.read(run_id).manifest, report)
    snapshot = ledger.record_independent_audit(
        run_id,
        expected_generation=expected_generation,
        lease_owner=lease_owner,
        auditor_id=assessment.auditor_id,
        phase="workload",
        result=assessment.result,
        complete=assessment.complete,
        inventory_sha256=assessment.inventory_sha256,
        evidence_sha256=assessment.evidence_sha256,
        checks=assessment.checks,
        observed_at=assessment.observed_at,
        now=now,
    )
    return snapshot, assessment
