"""Validate normalized read-only GCP inventory evidence for one run.

This module has no cloud client. A separately authenticated adapter must query
each required surface, fully paginate its results, and normalize resources into
the report format. Missing or partial coverage cannot produce a passing audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re

from run_contract import ContractError, validate_manifest, validate_storage_object_id
from run_ledger import LedgerSnapshot, RunLedger
from gcp_token_identity import PRINCIPAL_EMAIL


AUDIT_SCHEMA = "portfolio.gcp-inventory.v3"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
AUDITOR_ID = re.compile(r"^google-user-[A-Za-z0-9._~-]{1,255}$")
RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:/._@+=,%~-]{0,4095}$")

# These surfaces follow the current resource/ownership matrix. The GCP adapter
# must prove that every call covers every page and include a response
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

DISCOVERABLE_TYPES = {
    "compute.networks": "gcp.compute.network",
    "compute.subnetworks": "gcp.compute.subnetwork",
    "compute.routers": "gcp.compute.router",
    "compute.router_nats": "gcp.compute.router_nat",
    "container.clusters": "gcp.gke.cluster",
    "container.node_pools": "gcp.gke.node_pool",
    "gkehub.feature_memberships": "gcp.gkehub.feature_membership",
    "gkehub.memberships": "gcp.gkehub.membership",
    "pubsub.subscriptions": "gcp.pubsub.subscription",
    "pubsub.topics": "gcp.pubsub.topic",
    "storage.object_versions": "gcp.storage.object",
}

REPORT_FIELDS = {
    "schema", "run_id", "project_id", "manifest_sha256", "observed_at",
    "auditor_id", "auditor_email", "coverage", "resources",
}
COVERAGE_FIELDS = {"complete", "pages", "resource_count", "response_sha256"}
RESOURCE_FIELDS = {"surface", "project_id", "id"}


class AuditReportError(ValueError):
    """A normalized audit report is malformed or crosses the requested scope."""


def _valid_resource_id(
    resource_id: object,
    *,
    surface: str,
    project_id: str,
    run_id: str,
) -> bool:
    if not isinstance(resource_id, str) or RESOURCE_ID.fullmatch(resource_id) is None:
        return False
    if surface in {"storage.objects", "storage.object_versions"}:
        try:
            validate_storage_object_id(
                resource_id, project_id, run_id, require_run_id=False
            )
        except ContractError:
            return False
        if surface == "storage.objects" and not resource_id.endswith("@live"):
            return False
        if surface == "storage.object_versions" and resource_id.endswith("@live"):
            return False
        return True
    return "%" not in resource_id and not resource_id.startswith("gs://")


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


@dataclass(frozen=True)
class DiscoveryRegistration:
    """Result of appending exact run-owned discoveries before cleanup."""

    snapshot: LedgerSnapshot
    assessment: AuditAssessment
    registered_count: int
    unregistered_count: int


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
        raise AuditReportError(
            "audit report auditor_id must be a verified Google user subject"
        )
    auditor_email = report["auditor_email"]
    if (not isinstance(auditor_email, str)
            or PRINCIPAL_EMAIL.fullmatch(auditor_email) is None):
        raise AuditReportError("audit report auditor_email must be a verified user email")
    cleanup_principal = run["cleanup"]["service_account"]
    scheduler_principal = (
        f"{run['run_id']}-sched@{run['scope']['recovery_project_id']}"
        ".iam.gserviceaccount.com"
    )
    if auditor_email in {cleanup_principal, scheduler_principal}:
        raise AuditReportError(
            "workload auditor must be distinct from reaper and scheduler identities"
        )
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
        if not _valid_resource_id(
            resource_id,
            surface=surface,
            project_id=project_id,
            run_id=run["run_id"],
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
            "auditor_email": auditor_email,
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


def register_discovered_workload_resources(
    ledger: RunLedger,
    run_id: str,
    collector,
    *,
    expected_auditor_email: str,
    now: datetime | None = None,
) -> DiscoveryRegistration:
    """Collect complete authenticated inventory and register only run-owned IDs.

    The read-only collector verifies the human auditor principal with Google's
    UserInfo endpoint. The function then takes that auditor's separate ledger
    lease, checks that the source manifest has not changed, and appends exact
    eligible resources. The caller must run the reaper only after this function
    releases the auditor lease. Unowned or unsupported assets are fingerprinted
    as unregistered and remain visible to the later independent absence audit.
    """
    at = now or datetime.now(timezone.utc)
    if at.tzinfo is None or at.utcoffset() is None:
        raise AuditReportError("inventory registration time must include a timezone")
    before = ledger.read(run_id)
    if before.manifest["state"] != "cleaning":
        raise AuditReportError("discovered resources can only be registered during cleaning")
    observed_at = at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    report = collector.collect(
        before.manifest,
        observed_at=observed_at,
        expected_auditor_email=expected_auditor_email,
    )
    assessment = assess_workload_inventory(before.manifest, report)
    if not assessment.complete:
        raise AuditReportError("incomplete inventory cannot extend the run delete plan")

    project_id = before.manifest["scope"]["project_id"]
    run_id = before.manifest["run_id"]
    existing = {
        item["id"]: item
        for item in before.manifest["resources"]
    }
    registered = []
    unregistered = []
    project_create_proven = any(
        event["kind"] == "intent"
        and event["action"] == "create"
        and event["resource_type"] == "gcp.resourcemanager.project"
        and event["resource_id"] == f"projects/{project_id}"
        and any(
            result["kind"] == "result"
            and result["operation_id"] == event["operation_id"]
            and result["outcome"] in {"succeeded", "reconciled_present"}
            for result in before.manifest["operations"]
        )
        for event in before.manifest["operations"]
    )
    for discovered in report["resources"]:
        surface = discovered["surface"]
        resource_id = discovered["id"]
        resource_type = DISCOVERABLE_TYPES.get(surface)
        ownership_proof = None
        if surface == "gkehub.features":
            expected_feature = (
                f"projects/{project_id}/locations/global/features/configmanagement"
            )
            if resource_id == expected_feature and project_create_proven:
                resource_type = "gcp.gkehub.feature"
                ownership_proof = "run_project"
        elif resource_type is not None:
            if surface == "storage.object_versions":
                try:
                    validate_storage_object_id(resource_id, project_id, run_id)
                except ContractError:
                    resource_type = None
                else:
                    ownership_proof = "run_name"
            elif run_id in resource_id:
                ownership_proof = "run_name"
            else:
                resource_type = None

        prior = existing.get(resource_id)
        if prior is not None:
            if prior["type"] == resource_type and prior["status"] != "deleted":
                continue
            unregistered.append(discovered)
            continue
        if resource_type is None or ownership_proof is None:
            unregistered.append(discovered)
            continue
        registered.append({
            "type": resource_type,
            "id": resource_id,
            "ownership_proof": ownership_proof,
            "status": "present",
            "delete_attempts": 0,
        })

    registered.sort(key=lambda item: (item["type"], item["id"]))
    unregistered.sort(key=lambda item: (item["surface"], item["id"]))
    if len(registered) > 256:
        raise AuditReportError("discovered cleanup target count exceeds the bounded ledger packet")
    unregistered_sha256 = _digest(unregistered)
    lease_owner = f"auditor:{assessment.auditor_id}"
    leased = ledger.acquire_lease(
        run_id,
        expected_generation=before.generation,
        owner=lease_owner,
        now=at,
    )
    try:
        registered_snapshot = ledger.record_discovered_resources(
            run_id,
            expected_generation=leased.generation,
            lease_owner=lease_owner,
            auditor_id=assessment.auditor_id,
            observed_at=assessment.observed_at,
            source_manifest_sha256=report["manifest_sha256"],
            evidence_sha256=assessment.evidence_sha256,
            resources=registered,
            operation_count=len(before.manifest["operations"]),
            unregistered_resource_count=len(unregistered),
            unregistered_resources_sha256=unregistered_sha256,
            now=at,
        )
    finally:
        latest = ledger.read(run_id)
        if latest.metadata["lease"]["owner"] == lease_owner:
            ledger.release_lease(
                run_id,
                expected_generation=latest.generation,
                owner=lease_owner,
                now=at,
            )
    return DiscoveryRegistration(
        snapshot=ledger.read(run_id),
        assessment=assessment,
        registered_count=len(registered),
        unregistered_count=len(unregistered),
    )
