"""Credential-free validation for GCP lab run manifests and cleanup plans.

This module validates intent and produces a stable cleanup order. It does not
authenticate, call Google Cloud APIs, persist a ledger, or delete resources.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from urllib.parse import quote, unquote


SCHEMA = "portfolio.gcp-run.v4"
MAX_RUN_LIFETIME = timedelta(minutes=60)
MAX_OPERATION_ATTEMPTS = 3
MAX_MANIFEST_RESOURCES = 512
MAX_DISCOVERY_RECEIPTS = 64
MAX_DISCOVERED_PER_RECEIPT = 256
RUN_ID = re.compile(r"^lab-[a-z0-9](?:[a-z0-9-]{0,17}[a-z0-9])?$")
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST = re.compile(r"^ghcr\.io/[a-z0-9-]+/[a-z0-9-]+@sha256:[0-9a-f]{64}$")
REAPER_IMAGE_DIGEST = re.compile(r"^ghcr\.io/[a-z0-9-]+/gcp-reaper@sha256:[0-9a-f]{64}$")
RESOURCE_URI = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:/._@+=,%~-]{0,4095}$")
GENERATION = re.compile(r"^[1-9][0-9]*$")
BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$")
CRC32C = re.compile(r"^[A-Za-z0-9+/]{6}==$")
BILLING_ACCOUNT = re.compile(
    r"^billingAccounts/[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$"
)
PARENT = re.compile(r"^(?:organizations|folders)/[0-9]{1,20}$")
RETIREMENT_PRINCIPAL_ID = re.compile(
    r"^(?:google-user-[A-Za-z0-9._~-]{1,255}|google-service-account-[0-9]{1,30})$"
)

REGION = "europe-west2"
ZONES = frozenset(
    {"europe-west2-a", "europe-west2-b", "europe-west2-c"}
)

STATES = frozenset(
    {
        "control_bootstrapping",
        "prepared",
        "project_create_requested",
        "project_created",
        "billing_link_requested",
        "project_linked",
        "api_enablement",
        "provisioning",
        "ready",
        "teardown_requested",
        "cleaning",
        "auditing",
        "project_retiring",
        "controls_reclaiming",
        "closed",
        "incomplete",
    }
)

TRANSITIONS = {
    "control_bootstrapping": {"prepared", "incomplete"},
    "prepared": {"project_create_requested", "teardown_requested", "incomplete"},
    "project_create_requested": {"project_created", "teardown_requested", "incomplete"},
    "project_created": {"billing_link_requested", "teardown_requested", "incomplete"},
    "billing_link_requested": {"project_linked", "teardown_requested", "incomplete"},
    "project_linked": {"api_enablement", "teardown_requested", "incomplete"},
    "api_enablement": {"provisioning", "teardown_requested", "incomplete"},
    "provisioning": {"ready", "teardown_requested", "incomplete"},
    "ready": {"teardown_requested", "incomplete"},
    "teardown_requested": {"cleaning", "incomplete"},
    "cleaning": {"auditing", "incomplete"},
    "auditing": {"cleaning", "project_retiring", "incomplete"},
    "project_retiring": {"controls_reclaiming", "incomplete"},
    "controls_reclaiming": {"closed", "incomplete"},
    "incomplete": {
        "project_created", "project_linked", "api_enablement", "provisioning",
        "teardown_requested", "cleaning", "auditing", "project_retiring",
        "controls_reclaiming",
    },
    "closed": set(),
}

# Larger values are deleted earlier. Only run-project resources the reaper owns
# are admitted here. Project IAM and recovery-project controls are separate.
DELETE_ORDER = {
    "gcp.storage.object": 220,
    "gcp.pubsub.subscription": 210,
    "gcp.pubsub.topic": 200,
    "gcp.gkehub.feature_membership": 190,
    "gcp.gkehub.membership": 180,
    "gcp.gkehub.feature": 170,
    "gcp.gke.node_pool": 160,
    "gcp.gke.cluster": 150,
    "gcp.compute.router_nat": 110,
    "gcp.compute.router": 100,
    "gcp.compute.subnetwork": 90,
    "gcp.compute.network": 80,
}
REQUESTABLE_RESOURCE_TYPES = frozenset(DELETE_ORDER) | {
    "gcp.resourcemanager.project",
    "gcp.billing.project_billing_info",
    "gcp.serviceusage.service",
}
OPERATION_ID = re.compile(r"^op-[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
OPERATION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_-]{0,255}$")
API_SERVICE = re.compile(r"^[a-z][a-z0-9-]*\.googleapis\.com$")

# These recovery-project controls are inventoried before creation, but only a
# separate post-audit operator can remove them. The Terraform state bucket and
# shared ledger are intentionally not in this deletion contract.
CONTROL_DELETE_ORDER = {
    "gcp.scheduler.job": 40,
    "gcp.run.job": 30,
    "gcp.iam.service_account": 20,
}

OWNERSHIP_PROOFS = frozenset({"run_label", "run_name", "run_project", "manifest_record"})
REGIONAL_RESOURCE_TYPES = frozenset(
    {
        "gcp.compute.subnetwork",
        "gcp.compute.router",
        "gcp.compute.router_nat",
        "gcp.gke.cluster",
        "gcp.gke.node_pool",
    }
)


class ContractError(ValueError):
    """An intent or manifest violates the reviewed cleanup contract."""


def retirement_lease_matches_principal(lease_owner: str, principal_id: str) -> bool:
    """Bind human and per-run janitor lease names to a verified principal ID."""
    if lease_owner == "operator:project-reclaimer":
        return (
            isinstance(principal_id, str)
            and principal_id.startswith("google-user-")
        )
    return janitor_lease_principal(lease_owner) == principal_id


def janitor_lease_principal(lease_owner: object) -> str | None:
    """Extract the principal from a unique Cloud Run execution lease owner."""
    if not isinstance(lease_owner, str):
        return None
    match = re.fullmatch(
        r"janitor:(google-service-account-[0-9]{1,30}):([a-z0-9-]{1,63})",
        lease_owner,
    )
    return match.group(1) if match else None


def _mapping(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{field} must be a non-empty string")
    return value


def _timestamp(value: object, field: str) -> datetime:
    raw = _string(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _finite_positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ContractError(f"{field} must be a finite positive number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ContractError(f"{field} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ContractError(f"{field} must be a finite positive number")
    return number


def _is_finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _only_keys(data: dict, allowed: set[str], field: str) -> None:
    extra = set(data) - allowed
    if extra:
        raise ContractError(f"{field} contains unsupported fields: {sorted(extra)}")


def _audit_summary(receipt: dict) -> dict:
    """Return the small manifest projection of the latest immutable audit receipt."""
    summary = {
        "result": receipt["result"],
        "complete": receipt["complete"],
        "resources_absent": receipt["checks"]["resources_absent"],
        "phase": receipt["phase"],
        "auditor_id": receipt["auditor_id"],
        "observed_at": receipt["observed_at"],
        "inventory_sha256": receipt["inventory_sha256"],
        "evidence_sha256": receipt["evidence_sha256"],
        "manifest_generation": receipt["manifest_generation"],
    }
    for key in (
        "project_absent", "project_services_reconciled", "billing_unlinked",
        "budget_reconciled",
        "recovery_controls_absent",
    ):
        if key in receipt["checks"]:
            summary[key] = receipt["checks"][key]
    return summary


def operation_id(
    run_id: str,
    attempt: int,
    action: str,
    resource_type: str,
    resource_id: str,
    request_sha256: str,
    phase: str,
) -> str:
    """Return the deterministic ID for one exact provider request attempt."""
    identity = {
        "run_id": run_id,
        "attempt": attempt,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "request_sha256": request_sha256,
        "phase": phase,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "op-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_intent(
    intent: object,
    *,
    now: datetime | None = None,
    new_run: bool = True,
) -> dict:
    """Validate a proposed GCP run before any provider mutation."""
    data = _mapping(intent, "intent")
    _only_keys(
        data,
        {
            "schema", "run_id", "scope", "created_at", "deadline_at", "source",
            "finance", "cleanup", "state", "resources", "controls",
            "independent_audit", "audit_receipts", "discovery_receipts",
            "recovery_control_receipts",
            "terraform_state", "operations",
        },
        "intent",
    )
    if data.get("schema") != SCHEMA:
        raise ContractError("schema is unsupported")

    run_id = _string(data.get("run_id"), "run_id")
    if not RUN_ID.fullmatch(run_id):
        raise ContractError("run_id must be a bounded lab-prefixed identifier")

    scope = _mapping(data.get("scope"), "scope")
    _only_keys(
        scope,
        {
            "project_id", "recovery_project_id", "ledger_bucket_name",
            "parent", "region", "zones", "enabled_api_services",
        },
        "scope",
    )
    project_id = _string(scope.get("project_id"), "scope.project_id")
    if not PROJECT_ID.fullmatch(project_id):
        raise ContractError("scope.project_id is invalid")
    recovery_project_id = _string(
        scope.get("recovery_project_id"), "scope.recovery_project_id"
    )
    if not PROJECT_ID.fullmatch(recovery_project_id):
        raise ContractError("scope.recovery_project_id is invalid")
    if recovery_project_id == project_id:
        raise ContractError("run and recovery projects must be separate")
    ledger_bucket_name = _string(
        scope.get("ledger_bucket_name"), "scope.ledger_bucket_name"
    )
    if (
        not BUCKET_NAME.fullmatch(ledger_bucket_name)
        or len(ledger_bucket_name) > 63
        or ledger_bucket_name.startswith("goog")
        or "google" in ledger_bucket_name
    ):
        raise ContractError("scope.ledger_bucket_name is invalid")
    parent = _string(scope.get("parent"), "scope.parent")
    if not PARENT.fullmatch(parent):
        raise ContractError("scope.parent must be a folder or organization resource name")
    if scope.get("region") != REGION:
        raise ContractError("scope.region must be the approved London region")
    zones = scope.get("zones")
    if (
        not isinstance(zones, list)
        or len(zones) != 3
        or not all(isinstance(zone, str) for zone in zones)
        or len(set(zones)) != 3
        or frozenset(zones) != ZONES
    ):
        raise ContractError("scope.zones must name the three London zones")
    enabled_services = scope.get("enabled_api_services")
    if (
        not isinstance(enabled_services, list)
        or not enabled_services
        or len(enabled_services) > 64
        or any(
            not isinstance(service, str) or not API_SERVICE.fullmatch(service)
            for service in enabled_services
        )
        or len(set(enabled_services)) != len(enabled_services)
        or enabled_services != sorted(enabled_services)
    ):
        raise ContractError(
            "scope.enabled_api_services must be a sorted, unique list of "
            "API service names"
        )

    started = _timestamp(data.get("created_at"), "created_at")
    deadline = _timestamp(data.get("deadline_at"), "deadline_at")
    current = now.astimezone(timezone.utc) if now and now.tzinfo else now
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ContractError("now must include a timezone")
    if deadline <= started:
        raise ContractError("deadline_at must be after created_at")
    if deadline - started > MAX_RUN_LIFETIME:
        raise ContractError("run lifetime cannot exceed 60 minutes")
    if current is not None and started > current + timedelta(minutes=2):
        raise ContractError("created_at cannot be materially in the future")
    if current is not None and deadline <= current:
        raise ContractError("deadline_at must still be in the future")

    source = _mapping(data.get("source"), "source")
    _only_keys(source, {"revision", "image_digest"}, "source")
    revision = _string(source.get("revision"), "source.revision")
    if not REVISION.fullmatch(revision):
        raise ContractError("source.revision must be a full immutable Git SHA")
    image = _string(source.get("image_digest"), "source.image_digest")
    if not IMAGE_DIGEST.fullmatch(image):
        raise ContractError("source.image_digest must be an immutable GHCR digest")

    _validate_terraform_state(data.get("terraform_state"), run_id)
    _validate_operations(
        data.get("operations"), run_id, project_id, allow_empty=new_run
    )

    finance = _mapping(data.get("finance"), "finance")
    _only_keys(
        finance,
        {
            "currency", "gross_estimate_gbp", "net_cash_exposure_gbp",
            "available_credit_gbp", "credit_observed_at", "credit_expires_at",
            "monthly_alert_threshold_gbp", "alerts_verified", "billing_account_id",
        },
        "finance",
    )
    if finance.get("currency") != "GBP":
        raise ContractError("finance.currency must be GBP")
    billing_account_id = _string(
        finance.get("billing_account_id"), "finance.billing_account_id"
    )
    if not BILLING_ACCOUNT.fullmatch(billing_account_id):
        raise ContractError(
            "finance.billing_account_id must be a canonical Cloud Billing account name"
        )
    _finite_positive_number(
        finance.get("gross_estimate_gbp"), "finance.gross_estimate_gbp"
    )
    net_cash = finance.get("net_cash_exposure_gbp")
    try:
        net_cash_value = float(net_cash)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError(
            "finance.net_cash_exposure_gbp must be finite and non-negative"
        ) from exc
    if (
        isinstance(net_cash, bool)
        or not isinstance(net_cash, (float, int))
        or not math.isfinite(net_cash_value)
        or net_cash_value < 0
    ):
        raise ContractError(
            "finance.net_cash_exposure_gbp must be finite and non-negative"
        )
    available_credit = finance.get("available_credit_gbp")
    if (
        isinstance(available_credit, bool)
        or not isinstance(available_credit, (float, int))
        or not _is_finite(available_credit)
        or available_credit < 0
    ):
        raise ContractError("finance.available_credit_gbp must be finite and non-negative")
    credit_observed = _timestamp(
        finance.get("credit_observed_at"), "finance.credit_observed_at"
    )
    credit_expires = _timestamp(
        finance.get("credit_expires_at"), "finance.credit_expires_at"
    )
    if credit_expires <= deadline:
        raise ContractError("observed credits must remain valid through the run deadline")
    if credit_observed > started + timedelta(minutes=2):
        raise ContractError("credit observation cannot postdate run creation")
    _finite_positive_number(
        finance.get("monthly_alert_threshold_gbp"),
        "finance.monthly_alert_threshold_gbp",
    )
    if finance.get("alerts_verified") is not True:
        raise ContractError("finance.alerts_verified must be true before activation")

    cleanup = _mapping(data.get("cleanup"), "cleanup")
    _only_keys(
        cleanup,
        {
            "service_account", "image_digest", "identity_audience", "identity_verified",
            "independent_recovery_verified",
        },
        "cleanup",
    )
    cleanup_account = _string(
        cleanup.get("service_account"), "cleanup.service_account"
    )
    if not cleanup_account.endswith(
        f"@{recovery_project_id}.iam.gserviceaccount.com"
    ):
        raise ContractError(
            "cleanup.service_account must belong to the recovery project"
        )
    reaper_image_digest = _string(
        cleanup.get("image_digest"), "cleanup.image_digest"
    )
    if not REAPER_IMAGE_DIGEST.fullmatch(reaper_image_digest):
        raise ContractError("cleanup.image_digest must pin the gcp-reaper image by SHA-256")
    if cleanup.get("identity_verified") is not True:
        raise ContractError("cleanup.identity_verified must be true")
    identity_audience = _string(
        cleanup.get("identity_audience"), "cleanup.identity_audience"
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}", identity_audience):
        raise ContractError("cleanup.identity_audience is malformed")
    if cleanup.get("independent_recovery_verified") is not True:
        raise ContractError("independent cleanup recovery must be verified")

    controls = data.get("controls")
    if not isinstance(controls, list):
        raise ContractError("controls must be a list")
    _validate_controls(
        controls, recovery_project_id, run_id, expected_only=new_run
    )
    expected_reaper = (
        f"projects/{recovery_project_id}/serviceAccounts/"
        f"{run_id}-reaper@{recovery_project_id}.iam.gserviceaccount.com"
    )
    expected_reaper_email = expected_reaper.rsplit("/", 1)[1]
    if cleanup_account != expected_reaper_email or not any(
        item.get("type") == "gcp.iam.service_account"
        and item.get("id") == expected_reaper
        for item in controls
    ):
        raise ContractError(
            "cleanup identity must match the predeclared per-run reaper account"
        )
    if new_run:
        if data.get("state") != "control_bootstrapping":
            raise ContractError("new intent must start in control_bootstrapping state")
        if data.get("resources") != []:
            raise ContractError("new intent must have an empty workload resource list")
        if data.get("operations") != []:
            raise ContractError("new intent must have an empty provider operation history")
        if data.get("audit_receipts", []) != [] or data.get("independent_audit") is not None:
            raise ContractError("new intent cannot contain independent audit results")
        if data.get("discovery_receipts", []) != []:
            raise ContractError("new intent cannot contain discovery receipts")
        if data.get("recovery_control_receipts", []) != []:
            raise ContractError("new intent cannot contain recovery-control verification receipts")
    return data


def _validate_terraform_state(value: object, run_id: str) -> None:
    """Validate exact backend/backup object references and restore evidence."""
    state = _mapping(value, "terraform_state")
    _only_keys(state, {"backend", "backup", "restore_proofs"}, "terraform_state")
    expected_object = f"runs/{run_id}/terraform.tfstate"
    bucket_names = set()
    for field in ("backend", "backup"):
        entry = _mapping(state.get(field), f"terraform_state.{field}")
        _only_keys(entry, {"bucket", "object", "generation"}, f"terraform_state.{field}")
        bucket = _string(entry.get("bucket"), f"terraform_state.{field}.bucket")
        if (
            not BUCKET_NAME.fullmatch(bucket)
            or len(bucket) > 63
            or bucket.startswith("goog")
            or "google" in bucket
        ):
            raise ContractError(f"terraform_state.{field}.bucket is invalid")
        if bucket in bucket_names:
            raise ContractError("Terraform state and backup buckets must be distinct")
        bucket_names.add(bucket)
        if entry.get("object") != expected_object:
            raise ContractError(
                f"terraform_state.{field}.object must be the exact run-scoped state path"
            )
        generation = entry.get("generation")
        if generation is not None and (
            not isinstance(generation, str) or not GENERATION.fullmatch(generation)
        ):
            raise ContractError(
                f"terraform_state.{field}.generation must be null or a positive generation string"
            )

    proofs = state.get("restore_proofs")
    if not isinstance(proofs, list) or len(proofs) > 32:
        raise ContractError("terraform_state.restore_proofs must contain at most 32 records")
    seen_proofs = set()
    for index, raw in enumerate(proofs):
        proof = _mapping(raw, f"terraform_state.restore_proofs[{index}]")
        _only_keys(
            proof,
            {
                "snapshot", "state_generation", "backup_generation",
                "restored_generation", "verified_at", "result",
                "state_crc32c", "backup_crc32c", "restored_crc32c",
            },
            f"terraform_state.restore_proofs[{index}]",
        )
        snapshot = proof.get("snapshot")
        if not isinstance(snapshot, str) or snapshot not in {"current", "previous"}:
            raise ContractError("restore proof snapshot must be current or previous")
        for key in ("state_generation", "backup_generation", "restored_generation"):
            generation = proof.get(key)
            if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
                raise ContractError(f"restore proof {key} must be a positive generation string")
        proof_identity = (
            snapshot,
            proof["state_generation"],
            proof["backup_generation"],
            proof["restored_generation"],
        )
        if proof_identity in seen_proofs:
            raise ContractError("restore proof generation tuple must be unique")
        seen_proofs.add(proof_identity)
        _timestamp(proof.get("verified_at"), f"restore_proofs[{index}].verified_at")
        if not isinstance(proof.get("result"), str) or proof["result"] not in {
            "passed", "failed"
        }:
            raise ContractError("restore proof result must be passed or failed")
        checksums = [proof.get(key) for key in (
            "state_crc32c", "backup_crc32c", "restored_crc32c"
        )]
        if any(not isinstance(checksum, str) or not CRC32C.fullmatch(checksum)
               for checksum in checksums):
            raise ContractError("restore proof CRC32C values must be base64-encoded checksums")
        if proof["result"] == "passed" and len(set(checksums)) != 1:
            raise ContractError("passed restore proof requires matching state, backup, and restore checksums")


def _validate_operation_target(
    action: str,
    resource_type: str,
    resource_id: str,
    phase: str,
    run_id: str,
    project_id: str,
) -> None:
    if not isinstance(resource_type, str):
        raise ContractError("provider operation resource type is unsupported")
    if not isinstance(action, str):
        raise ContractError("provider operation action must be create or delete")
    if resource_type not in REQUESTABLE_RESOURCE_TYPES:
        raise ContractError("provider operation resource type is unsupported")
    if action not in {"create", "delete"}:
        raise ContractError("provider operation action must be create or delete")

    if resource_type == "gcp.resourcemanager.project":
        expected_phase = (
            "project_create_requested" if action == "create" else "project_retiring"
        )
        if phase != expected_phase:
            raise ContractError("run project operation is invalid for this lifecycle phase")
    elif resource_type == "gcp.billing.project_billing_info":
        expected_phase = (
            "billing_link_requested" if action == "create" else "project_retiring"
        )
        if phase != expected_phase:
            raise ContractError("billing operation is invalid for this lifecycle phase")
    elif resource_type == "gcp.serviceusage.service":
        allowed_phases = (
            {"api_enablement"} if action == "create" else {"cleaning", "incomplete"}
        )
        if phase not in allowed_phases:
            raise ContractError("API-service operation is invalid for this lifecycle phase")
    else:
        allowed_phases = (
            {"provisioning"} if action == "create" else {"cleaning", "incomplete"}
        )
        if phase not in allowed_phases:
            raise ContractError("workload resource operation is invalid for this lifecycle phase")

    if resource_type in {
        "gcp.resourcemanager.project",
        "gcp.billing.project_billing_info",
    }:
        if resource_id != f"projects/{project_id}":
            raise ContractError("project operation target must match the exact run project")
        return

    if resource_type == "gcp.serviceusage.service":
        prefix = f"projects/{project_id}/services/"
        service = resource_id[len(prefix):] if resource_id.startswith(prefix) else ""
        if not API_SERVICE.fullmatch(service):
            raise ContractError("API enablement target must name one service in the run project")
        return

    project_segment = re.compile(rf"(?:^|/)projects/{re.escape(project_id)}/")
    if (
        not RESOURCE_URI.fullmatch(resource_id)
        or not project_segment.search(resource_id)
    ):
        raise ContractError("provider operation target must be a canonical URI in this run project")
    if resource_type == "gcp.storage.object":
        validate_storage_object_id(resource_id, project_id, run_id)
        return
    if "%" in resource_id:
        raise ContractError("percent-encoded segments are supported only for Storage objects")
    if resource_type in REGIONAL_RESOURCE_TYPES and not any(
        marker in resource_id
        for marker in (f"/regions/{REGION}/", f"/locations/{REGION}/")
    ):
        raise ContractError("regional provider operation must stay in the approved London region")
    if resource_type == "gcp.gkehub.feature":
        if not resource_id.endswith("/features/configmanagement"):
            raise ContractError("Config Sync Feature operation target is unsupported")
    elif run_id not in resource_id:
        raise ContractError("provider operation target must include the run ID")


def validate_storage_object_id(
    resource_id: str,
    project_id: str,
    run_id: str,
    *,
    require_run_id: bool = True,
) -> None:
    parts = resource_id.split("/")
    if (
        len(parts) != 6
        or parts[:3] != ["projects", project_id, "buckets"]
        or parts[4] != "objects"
        or re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", parts[3]) is None
    ):
        raise ContractError("Storage object ID must bind the exact project and bucket")
    try:
        encoded_name, version = parts[5].rsplit("@", 1)
        object_name = unquote(encoded_name, errors="strict")
    except (IndexError, UnicodeDecodeError, ValueError):
        raise ContractError("Storage object ID encoding is malformed") from None
    if (
        not object_name
        or quote(object_name, safe="") != encoded_name
        or len(object_name.encode("utf-8")) > 1024
        or "\x00" in object_name
        or (version != "live" and GENERATION.fullmatch(version) is None)
    ):
        raise ContractError("Storage object ID is not canonical")
    if require_run_id and run_id not in parts[3] and run_id not in object_name:
        raise ContractError("Storage object target must include the run ID")


def _validate_operations(
    value: object,
    run_id: str,
    project_id: str,
    *,
    allow_empty: bool,
) -> None:
    operations = value
    if not isinstance(operations, list) or len(operations) > 1024:
        raise ContractError("operations must contain at most 1024 append-only events")
    if allow_empty and operations:
        raise ContractError("new intent must start with an empty provider operation history")

    intents = {}
    latest_attempt = {}
    pending_target = set()
    previous_at = None
    retirement_actor_id = None
    for index, event in enumerate(operations, start=1):
        event = _mapping(event, f"operations[{index - 1}]")
        sequence = event.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != index:
            raise ContractError("provider operation event sequence must be contiguous")
        kind = event.get("kind")
        if kind == "intent":
            _only_keys(
                event,
                {
                    "sequence", "kind", "operation_id", "attempt", "action",
                    "resource_type", "resource_id", "request_sha256", "phase", "at",
                    "actor_id",
                },
                f"operations[{index - 1}]",
            )
            attempt = event.get("attempt")
            if (
                isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or attempt < 1
                or attempt > MAX_OPERATION_ATTEMPTS
            ):
                raise ContractError(
                    f"provider operation attempt must be from 1 to {MAX_OPERATION_ATTEMPTS}"
                )
            action = event.get("action")
            resource_type = event.get("resource_type")
            resource_id = _string(event.get("resource_id"), "operation.resource_id")
            request_sha256 = event.get("request_sha256")
            if not isinstance(request_sha256, str) or not SHA256.fullmatch(request_sha256):
                raise ContractError("operation request_sha256 must be a SHA-256 hex digest")
            phase = event.get("phase")
            if not isinstance(phase, str) or phase not in STATES:
                raise ContractError("provider operation phase is unsupported")
            _validate_operation_target(
                action, resource_type, resource_id, phase, run_id, project_id
            )
            retiring_delete = (
                phase == "project_retiring"
                and action == "delete"
                and resource_type in {
                    "gcp.billing.project_billing_info",
                    "gcp.resourcemanager.project",
                }
            )
            actor_id = event.get("actor_id")
            if retiring_delete and (
                not isinstance(actor_id, str)
                or RETIREMENT_PRINCIPAL_ID.fullmatch(actor_id) is None
            ):
                raise ContractError(
                    "project retirement operation requires a verified Google principal ID"
                )
            if retiring_delete:
                if retirement_actor_id is None:
                    retirement_actor_id = actor_id
                elif actor_id != retirement_actor_id:
                    raise ContractError(
                        "project retirement operations must retain the same verified Google principal"
                    )
            if not retiring_delete and "actor_id" in event:
                raise ContractError("actor_id is unsupported for this provider operation")
            target = (action, resource_type, resource_id)
            if target in pending_target:
                raise ContractError("cannot issue a new provider request while the previous attempt is unresolved")
            expected_attempt = latest_attempt.get(target, 0) + 1
            if attempt != expected_attempt:
                raise ContractError("provider operation attempts must be consecutive")
            expected_id = operation_id(
                run_id, attempt, action, resource_type, resource_id, request_sha256, phase
            )
            op_id = event.get("operation_id")
            if not isinstance(op_id, str) or not OPERATION_ID.fullmatch(op_id) or op_id != expected_id:
                raise ContractError("provider operation ID must match its deterministic request identity")
            if op_id in intents:
                raise ContractError("provider operation ID cannot be reused")
            at = _timestamp(event.get("at"), f"operations[{index - 1}].at")
            if previous_at is not None and at < previous_at:
                raise ContractError("provider operation timestamps must be monotonic")
            intents[op_id] = {
                "action": action,
                "at": at,
                "target": target,
                "provider_operation_name": None,
                "progress_count": 0,
            }
            latest_attempt[target] = attempt
            pending_target.add(target)
            previous_at = at
        elif kind == "progress":
            _only_keys(
                event,
                {
                    "sequence", "kind", "operation_id", "provider_operation_name",
                    "response_sha256", "at",
                },
                f"operations[{index - 1}]",
            )
            op_id = event.get("operation_id")
            if not isinstance(op_id, str) or op_id not in intents:
                raise ContractError("provider progress must refer to a prior exact operation intent")
            intent = intents[op_id]
            if intent.get("resolved"):
                raise ContractError("completed provider operation cannot accept more progress")
            provider_name = event.get("provider_operation_name")
            if not isinstance(provider_name, str) or not OPERATION_NAME.fullmatch(provider_name):
                raise ContractError("provider progress operation name is malformed")
            if (
                intent["provider_operation_name"] is not None
                and intent["provider_operation_name"] != provider_name
            ):
                raise ContractError("provider operation name cannot change during reconciliation")
            response_sha256 = event.get("response_sha256")
            if not isinstance(response_sha256, str) or not SHA256.fullmatch(response_sha256):
                raise ContractError("provider progress summary must be SHA-256 fingerprinted")
            at = _timestamp(event.get("at"), f"operations[{index - 1}].at")
            if at < intent["at"] or (previous_at is not None and at < previous_at):
                raise ContractError("provider progress timestamp must follow its intent monotonically")
            intent["provider_operation_name"] = provider_name
            intent["progress_count"] += 1
            previous_at = at
        elif kind == "result":
            _only_keys(
                event,
                {
                    "sequence", "kind", "operation_id", "outcome",
                    "provider_operation_name", "response_sha256", "at",
                },
                f"operations[{index - 1}]",
            )
            op_id = event.get("operation_id")
            if not isinstance(op_id, str) or op_id not in intents:
                raise ContractError("provider result must refer to a prior exact operation intent")
            intent = intents[op_id]
            if intent.get("resolved"):
                raise ContractError("provider operation intent already has a result")
            outcome = event.get("outcome")
            if not isinstance(outcome, str) or outcome not in {
                "succeeded", "failed", "reconciled_present", "reconciled_absent"
            }:
                raise ContractError("provider operation result is unsupported")
            action = intent["target"][0]
            if outcome == "reconciled_present" and action != "create":
                raise ContractError("reconciled_present applies only to a create intent")
            if outcome == "reconciled_absent" and action != "delete":
                raise ContractError("reconciled_absent applies only to a delete intent")
            provider_name = event.get("provider_operation_name")
            if provider_name is not None and (
                not isinstance(provider_name, str)
                or not OPERATION_NAME.fullmatch(provider_name)
            ):
                raise ContractError("provider operation name is malformed")
            if intent["provider_operation_name"] is not None and (
                provider_name != intent["provider_operation_name"]
            ):
                raise ContractError("provider result must retain its journaled operation name")
            response_sha256 = event.get("response_sha256")
            if not isinstance(response_sha256, str) or not SHA256.fullmatch(response_sha256):
                raise ContractError("provider response summary must be SHA-256 fingerprinted")
            at = _timestamp(event.get("at"), f"operations[{index - 1}].at")
            if at < intent["at"] or (previous_at is not None and at < previous_at):
                raise ContractError("provider result timestamp must follow its intent monotonically")
            intent["resolved"] = True
            pending_target.remove(intent["target"])
            previous_at = at
        else:
            raise ContractError("provider operation event kind is unsupported")


def _validate_controls(
    controls: list,
    recovery_project_id: str,
    run_id: str,
    *,
    expected_only: bool = False,
) -> None:
    if not controls:
        raise ContractError("controls must list expected per-run recovery resources")
    seen = set()
    seen_types = set()
    service_accounts = set()
    for index, raw in enumerate(controls):
        item = _mapping(raw, f"controls[{index}]")
        _only_keys(
            item,
            {"type", "id", "ownership_proof", "status", "delete_attempts"},
            f"controls[{index}]",
        )
        resource_type = _string(item.get("type"), f"controls[{index}].type")
        if resource_type not in CONTROL_DELETE_ORDER:
            raise ContractError(f"control resource type is unsupported: {resource_type}")
        resource_id = _string(item.get("id"), f"controls[{index}].id")
        project_segment = re.compile(
            rf"(?:^|/)projects/{re.escape(recovery_project_id)}/"
        )
        if (
            not RESOURCE_URI.fullmatch(resource_id)
            or not project_segment.search(resource_id)
        ):
            raise ContractError("control id must belong to the recovery project")
        if run_id not in resource_id:
            raise ContractError("control id must include the run id")
        if resource_type in {"gcp.run.job", "gcp.scheduler.job"} and (
            f"/locations/{REGION}/" not in resource_id
        ):
            raise ContractError("regional recovery controls must stay in London")
        if resource_type == "gcp.run.job" and not resource_id.endswith(
            f"/{run_id}-reaper"
        ):
            raise ContractError("reaper Job id must use the deterministic run name")
        if resource_type == "gcp.scheduler.job" and not resource_id.endswith(
            f"/{run_id}-expiry"
        ):
            raise ContractError("Scheduler id must use the deterministic run name")
        if resource_type == "gcp.iam.service_account":
            if not resource_id.endswith(
                (
                    f"/{run_id}-reaper@{recovery_project_id}.iam.gserviceaccount.com",
                    f"/{run_id}-sched@{recovery_project_id}.iam.gserviceaccount.com",
                )
            ):
                raise ContractError("service account id must use a deterministic run name")
            service_accounts.add(resource_id)
        if resource_id in seen:
            raise ContractError("control ids must be unique")
        seen.add(resource_id)
        seen_types.add(resource_type)
        if item.get("ownership_proof") != "run_name":
            raise ContractError("recovery control ownership must use deterministic run names")
        status = item.get("status")
        if not isinstance(status, str) or status not in {
            "expected", "present", "deleted"
        }:
            raise ContractError("control status is unsupported")
        if expected_only and status != "expected":
            raise ContractError("new intent controls must start as expected")
        attempts = item.get("delete_attempts", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ContractError("control delete_attempts must be a non-negative integer")
    required_types = {"gcp.run.job", "gcp.scheduler.job", "gcp.iam.service_account"}
    if seen_types != required_types or len(service_accounts) != 2:
        raise ContractError(
            "expected recovery controls must include one Job, one Scheduler, and two service accounts"
        )


def validate_manifest(manifest: object) -> dict:
    """Validate a persisted manifest before changing it or acting on resources."""
    data = _mapping(manifest, "manifest")
    validate_intent(data, new_run=False)
    if not isinstance(data.get("state"), str) or data["state"] not in STATES:
        raise ContractError("state is unsupported")

    resources = data.get("resources")
    if not isinstance(resources, list):
        raise ContractError("resources must be a list")
    if len(resources) > MAX_MANIFEST_RESOURCES:
        raise ContractError("resources exceed the bounded manifest inventory")
    recovery_project_id = data["scope"]["recovery_project_id"]
    controls = data.get("controls")
    if not isinstance(controls, list):
        raise ContractError("controls must be a list")
    _validate_controls(controls, recovery_project_id, data["run_id"])
    if data["state"] == "prepared" and any(
        control["status"] != "present" for control in controls
    ):
        raise ContractError("prepared state requires every recovery control verified present")
    control_receipts = data.get("recovery_control_receipts", [])
    if not isinstance(control_receipts, list) or len(control_receipts) > 8:
        raise ContractError("recovery_control_receipts must contain at most eight entries")
    previous_control_time = None
    control_receipt_fields = {
        "sequence", "run_id", "recovery_project_id", "verifier", "observed_at",
        "manifest_generation", "observations_sha256", "receipt_id",
    }
    for index, receipt in enumerate(control_receipts, start=1):
        receipt = _mapping(receipt, f"recovery_control_receipts[{index - 1}]")
        _only_keys(receipt, control_receipt_fields, f"recovery_control_receipts[{index - 1}]")
        if set(receipt) != control_receipt_fields:
            raise ContractError("recovery-control receipt fields are incomplete")
        if (
            isinstance(receipt["sequence"], bool)
            or not isinstance(receipt["sequence"], int)
            or receipt["sequence"] != index
            or receipt["run_id"] != data["run_id"]
            or receipt["recovery_project_id"] != recovery_project_id
            or receipt["verifier"] != "portfolio.gcp-recovery-controls.v1"
        ):
            raise ContractError("recovery-control receipt sequence, scope, or verifier is invalid")
        observed_at = _timestamp(receipt["observed_at"], "recovery-control receipt observed_at")
        if previous_control_time is not None and observed_at < previous_control_time:
            raise ContractError("recovery-control receipt timestamps must be monotonic")
        previous_control_time = observed_at
        for field in ("observations_sha256", "receipt_id"):
            if not isinstance(receipt[field], str) or not SHA256.fullmatch(receipt[field]):
                raise ContractError(f"recovery-control receipt {field} must be a SHA-256 digest")
        generation = receipt["manifest_generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ContractError("recovery-control receipt manifest_generation must be positive")
        receipt_payload = {key: value for key, value in receipt.items() if key != "receipt_id"}
        receipt_canonical = json.dumps(
            receipt_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if hashlib.sha256(receipt_canonical.encode("utf-8")).hexdigest() != receipt["receipt_id"]:
            raise ContractError("recovery-control receipt id does not match its contents")
    if data["state"] == "prepared" and not control_receipts:
        raise ContractError("prepared state requires a durable recovery-control verification receipt")
    if any(item["status"] == "present" for item in controls) and not control_receipts:
        raise ContractError("present recovery controls require a durable verification receipt")
    if data["state"] not in {"control_bootstrapping", "incomplete"} and not control_receipts:
        raise ContractError("post-bootstrap lifecycle states require a recovery-control verification receipt")
    if data["state"] in {
        "control_bootstrapping", "prepared", "project_create_requested",
        "project_created", "billing_link_requested", "project_linked",
        "api_enablement",
    } and resources:
        raise ContractError("workload resources cannot exist before API enablement completes")
    project_id = data["scope"]["project_id"]
    run_id = data["run_id"]
    ids = set()
    for index, resource in enumerate(resources):
        entry = _mapping(resource, f"resources[{index}]")
        _only_keys(
            entry,
            {"type", "id", "ownership_proof", "status", "delete_attempts"},
            f"resources[{index}]",
        )
        resource_type = _string(entry.get("type"), f"resources[{index}].type")
        if resource_type not in DELETE_ORDER:
            raise ContractError(
                f"resource type is not owned by the run reaper: {resource_type}"
            )
        resource_id = _string(entry.get("id"), f"resources[{index}].id")
        project_segment = re.compile(rf"(?:^|/)projects/{re.escape(project_id)}/")
        if (
            not RESOURCE_URI.fullmatch(resource_id)
            or not project_segment.search(resource_id)
        ):
            raise ContractError("resource id must be a canonical URI in this project")
        if resource_type == "gcp.storage.object":
            validate_storage_object_id(resource_id, project_id, run_id)
        elif "%" in resource_id:
            raise ContractError(
                "percent-encoded segments are supported only for Storage objects"
            )
        if resource_type in REGIONAL_RESOURCE_TYPES and not any(
            marker in resource_id
            for marker in (f"/regions/{REGION}/", f"/locations/{REGION}/")
        ):
            raise ContractError(
                "regional resource id must remain in the approved London region"
            )
        if resource_id in ids:
            raise ContractError("resource ids must be unique")
        ids.add(resource_id)
        proof = _string(entry.get("ownership_proof"), "ownership_proof")
        if proof not in OWNERSHIP_PROOFS:
            raise ContractError("resource ownership proof is unsupported")
        if resource_type == "gcp.gkehub.feature":
            if not resource_id.endswith("/features/configmanagement"):
                raise ContractError("Config Sync Feature id is unsupported")
            if proof != "run_project":
                raise ContractError("Feature ownership must use its dedicated run project")
        elif resource_type != "gcp.storage.object" and run_id not in resource_id:
            raise ContractError("run-scoped resource id must include the run id")
        if (
            not isinstance(entry.get("status"), str)
            or entry["status"] not in {"expected", "present", "deleted"}
        ):
            raise ContractError("resource status is unsupported")
        attempts = entry.get("delete_attempts", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ContractError("delete_attempts must be a non-negative integer")

    declared_resources = {(item["type"], item["id"]) for item in resources}
    for event in data["operations"]:
        if event["kind"] == "intent" and event["resource_type"] in DELETE_ORDER:
            target = (event["resource_type"], event["resource_id"])
            if target not in declared_resources:
                raise ContractError(
                    "provider operation target must be predeclared in the exact run inventory"
                )

    discovery_receipts = data.get("discovery_receipts", [])
    if not isinstance(discovery_receipts, list) or len(discovery_receipts) > MAX_DISCOVERY_RECEIPTS:
        raise ContractError("discovery_receipts exceed the bounded append-only history")
    previous_discovery_time = None
    discovered_ids = set()
    discovery_fields = {
        "sequence", "run_id", "project_id", "auditor_id", "observed_at",
        "manifest_sha256", "evidence_sha256", "registered_resources",
        "unregistered_resource_count", "unregistered_resources_sha256",
        "operation_count", "manifest_generation", "receipt_id",
    }
    declared_by_id = {(item["type"], item["id"]): item for item in resources}
    for index, receipt in enumerate(discovery_receipts, start=1):
        receipt = _mapping(receipt, f"discovery_receipts[{index - 1}]")
        _only_keys(receipt, discovery_fields, f"discovery_receipts[{index - 1}]")
        if set(receipt) != discovery_fields:
            raise ContractError("discovery receipt fields are incomplete")
        if (
            isinstance(receipt["sequence"], bool)
            or not isinstance(receipt["sequence"], int)
            or receipt["sequence"] != index
            or receipt["run_id"] != run_id
            or receipt["project_id"] != project_id
        ):
            raise ContractError("discovery receipt sequence and scope must match the run")
        auditor_id = receipt["auditor_id"]
        if not isinstance(auditor_id, str) or re.fullmatch(
            r"google-user-[A-Za-z0-9._~-]{1,255}", auditor_id
        ) is None:
            raise ContractError("discovery receipt auditor must be a Google user subject")
        observed_at = _timestamp(receipt["observed_at"], "discovery receipt observed_at")
        if previous_discovery_time is not None and observed_at < previous_discovery_time:
            raise ContractError("discovery receipt timestamps must be monotonic")
        previous_discovery_time = observed_at
        for field in ("manifest_sha256", "evidence_sha256", "unregistered_resources_sha256", "receipt_id"):
            if not isinstance(receipt[field], str) or not SHA256.fullmatch(receipt[field]):
                raise ContractError(f"discovery receipt {field} must be a SHA-256 digest")
        generation = receipt["manifest_generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ContractError("discovery receipt manifest_generation must be positive")
        operation_count = receipt["operation_count"]
        if (isinstance(operation_count, bool) or not isinstance(operation_count, int)
                or operation_count < 0):
            raise ContractError("discovery receipt operation_count must be non-negative")
        unregistered_count = receipt["unregistered_resource_count"]
        if (isinstance(unregistered_count, bool) or not isinstance(unregistered_count, int)
                or not 0 <= unregistered_count <= 100_000):
            raise ContractError("discovery receipt unregistered count is out of bounds")
        registered = receipt["registered_resources"]
        if not isinstance(registered, list) or len(registered) > MAX_DISCOVERED_PER_RECEIPT:
            raise ContractError("discovery receipt registered resources exceed the bound")
        prior_key = None
        for entry in registered:
            entry = _mapping(entry, "discovery receipt registered resource")
            _only_keys(entry, {"type", "id"}, "discovery receipt registered resource")
            if set(entry) != {"type", "id"}:
                raise ContractError("discovery receipt resource fields are incomplete")
            key = (entry["type"], entry["id"])
            if not all(isinstance(value, str) and value for value in key):
                raise ContractError("discovery receipt resource identity is malformed")
            if prior_key is not None and key <= prior_key:
                raise ContractError("discovery receipt resources must be unique and sorted")
            prior_key = key
            if key not in declared_by_id:
                raise ContractError("discovery receipt resource is absent from the manifest")
            if key in discovered_ids:
                raise ContractError("resource may be registered by discovery only once")
            discovered_ids.add(key)
        receipt_payload = {key: value for key, value in receipt.items() if key != "receipt_id"}
        receipt_canonical = json.dumps(
            receipt_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if hashlib.sha256(receipt_canonical.encode("utf-8")).hexdigest() != receipt["receipt_id"]:
            raise ContractError("discovery receipt id does not match its contents")
        if data["state"] not in {
            "cleaning", "auditing", "incomplete", "project_retiring",
            "controls_reclaiming", "closed",
        }:
            raise ContractError("discovery receipts require a teardown lifecycle state")

    receipts = data.get("audit_receipts", [])
    if not isinstance(receipts, list) or len(receipts) > 64:
        raise ContractError("audit_receipts must contain at most 64 entries")
    previous_time = None
    successful_workload_audit = False
    receipt_fields = {
        "sequence", "run_id", "project_id", "recovery_project_id", "phase",
        "result", "complete", "auditor_id", "observed_at",
        "inventory_sha256", "evidence_sha256", "manifest_generation", "checks",
        "receipt_id",
    }
    for index, receipt in enumerate(receipts, start=1):
        receipt = _mapping(receipt, f"audit_receipts[{index - 1}]")
        _only_keys(receipt, receipt_fields, f"audit_receipts[{index - 1}]")
        if set(receipt) != receipt_fields:
            raise ContractError("audit receipt fields are incomplete")
        if (
            receipt["run_id"] != data["run_id"]
            or receipt["project_id"] != data["scope"]["project_id"]
            or receipt["recovery_project_id"] != data["scope"]["recovery_project_id"]
        ):
            raise ContractError("audit receipt scope must match its exact run and projects")
        sequence = receipt["sequence"]
        if isinstance(sequence, bool) or sequence != index:
            raise ContractError("audit receipt sequence must be contiguous and monotonic")
        phase = receipt["phase"]
        if phase not in {"workload", "closure"}:
            raise ContractError("audit receipt phase is unsupported")
        if phase == "closure" and not successful_workload_audit:
            raise ContractError("closure audit requires a prior passed workload audit")
        expected_checks = (
            {"resource_inventory_complete", "resources_absent"}
            if phase == "workload"
            else {
                "resource_inventory_complete", "resources_absent", "project_absent",
                "project_services_reconciled", "billing_unlinked",
                "budget_reconciled", "recovery_controls_absent",
            }
        )
        checks = _mapping(receipt["checks"], "audit receipt checks")
        if set(checks) != expected_checks or any(
            not isinstance(value, bool) for value in checks.values()
        ):
            raise ContractError("audit receipt checks do not match its phase")
        if not isinstance(receipt["complete"], bool):
            raise ContractError("audit receipt complete must be boolean")
        result = receipt["result"]
        if result not in {"passed", "failed", "incomplete"}:
            raise ContractError("audit receipt result is unsupported")
        all_checks_passed = all(checks.values())
        if result == "passed" and (not receipt["complete"] or not all_checks_passed):
            raise ContractError("passed audit receipt must be complete with all checks passed")
        if result == "failed" and all_checks_passed:
            raise ContractError("failed audit receipt must contain a failed check")
        if result == "incomplete" and receipt["complete"]:
            raise ContractError("incomplete audit receipt cannot claim complete coverage")
        auditor_id = receipt["auditor_id"]
        if not isinstance(auditor_id, str) or re.fullmatch(
            r"google-user-[A-Za-z0-9._~-]{1,255}",
            auditor_id,
        ) is None:
            raise ContractError("audit receipt auditor_id must be a Google user subject")
        observed_at = _timestamp(receipt["observed_at"], "audit receipt observed_at")
        if previous_time is not None and observed_at < previous_time:
            raise ContractError("audit receipt timestamps must be monotonic")
        previous_time = observed_at
        for field in ("inventory_sha256", "evidence_sha256", "receipt_id"):
            if not isinstance(receipt[field], str) or not SHA256.fullmatch(receipt[field]):
                raise ContractError(f"audit receipt {field} must be a SHA-256 hex digest")
        receipt_payload = {key: value for key, value in receipt.items() if key != "receipt_id"}
        receipt_canonical = json.dumps(
            receipt_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if hashlib.sha256(receipt_canonical.encode("utf-8")).hexdigest() != receipt[
            "receipt_id"
        ]:
            raise ContractError("audit receipt id does not match its immutable contents")
        generation = receipt["manifest_generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ContractError("audit receipt manifest_generation must be positive")
        if phase == "workload" and data["state"] not in {
            "auditing", "cleaning", "incomplete", "project_retiring",
            "controls_reclaiming", "closed",
        }:
            raise ContractError("workload audit receipts require an audited lifecycle state")
        if phase == "closure" and data["state"] not in {"controls_reclaiming", "closed"}:
            raise ContractError("closure audit receipts require controls_reclaiming state")
        if phase == "workload" and result == "passed":
            if any(item["status"] != "deleted" for item in resources):
                raise ContractError("passed workload audit requires deleted inventory status")
            successful_workload_audit = True
        if phase == "closure" and any(
            item["status"] != "deleted" for item in resources + controls
        ):
            raise ContractError("closure audit requires workload and recovery controls deleted")

    audit = data.get("independent_audit")
    if receipts:
        expected_summary = _audit_summary(receipts[-1])
        if audit != expected_summary:
            raise ContractError("independent_audit must match the latest append-only receipt")
    elif audit is not None:
        raise ContractError("independent_audit requires an append-only audit receipt")

    passed_workload_audit = any(
        receipt["phase"] == "workload"
        and receipt["result"] == "passed"
        and receipt["complete"] is True
        and receipt["checks"].get("resources_absent") is True
        for receipt in receipts
    )
    if data["state"] in {"project_retiring", "controls_reclaiming", "closed"}:
        if not passed_workload_audit:
            raise ContractError(
                "project retirement requires a passed independent workload audit"
            )
        if any(resource["status"] != "deleted" for resource in resources):
            raise ContractError("project retirement requires every workload resource deleted")

    terminal_operations = {
        event["operation_id"]: event
        for event in data["operations"] if event["kind"] == "result"
    }
    successful_results = {"succeeded", "reconciled_absent"}
    successful_unlink = any(
        intent["kind"] == "intent"
        and intent["action"] == "delete"
        and intent["resource_type"] == "gcp.billing.project_billing_info"
        and intent["resource_id"] == f"projects/{project_id}"
        and intent["phase"] == "project_retiring"
        and terminal_operations.get(intent["operation_id"], {}).get("outcome")
        in successful_results
        for intent in data["operations"]
    )
    successful_project_delete = any(
        intent["kind"] == "intent"
        and intent["action"] == "delete"
        and intent["resource_type"] == "gcp.resourcemanager.project"
        and intent["resource_id"] == f"projects/{project_id}"
        and intent["phase"] == "project_retiring"
        and terminal_operations.get(intent["operation_id"], {}).get("outcome")
        in successful_results
        for intent in data["operations"]
    )
    unlink_result_sequence = {
        event["operation_id"]: event["sequence"]
        for event in data["operations"]
        if event["kind"] == "result"
        and event["outcome"] in successful_results
    }
    if any(
        intent["kind"] == "intent"
        and intent["action"] == "delete"
        and intent["resource_type"] == "gcp.resourcemanager.project"
        and intent["resource_id"] == f"projects/{project_id}"
        and (
            not successful_unlink
            or not any(
                unlink_intent["kind"] == "intent"
                and unlink_intent["action"] == "delete"
                and unlink_intent["resource_type"] == "gcp.billing.project_billing_info"
                and unlink_intent["resource_id"] == f"projects/{project_id}"
                and unlink_intent["phase"] == "project_retiring"
                and terminal_operations.get(unlink_intent["operation_id"], {}).get(
                    "outcome"
                ) in successful_results
                and unlink_result_sequence.get(unlink_intent["operation_id"], 0)
                < intent["sequence"]
                for unlink_intent in data["operations"]
            )
        )
        for intent in data["operations"]
    ):
        raise ContractError("project deletion requires a prior successful billing unlink")
    if data["state"] == "controls_reclaiming" and not successful_unlink:
        raise ContractError("recovery controls require a completed project billing unlink")
    if data["state"] in {"controls_reclaiming", "closed"} and not successful_project_delete:
        raise ContractError("recovery controls require independently reconciled project deletion")

    if data["state"] == "closed":
        if any(resource["status"] != "deleted" for resource in resources):
            raise ContractError("closed manifest must have no live run resources")
        if any(control["status"] != "deleted" for control in controls):
            raise ContractError("closed manifest requires recovery controls to be reclaimed")
        if (
            audit is None or audit.get("phase") != "closure"
            or audit.get("result") != "passed" or audit.get("complete") is not True
        ):
            raise ContractError("closed manifest requires a passed closure audit receipt")
    return data


def next_state(current: str, requested: str) -> str:
    """Return an allowed monotonic state transition or fail closed."""
    if not isinstance(current, str) or current not in TRANSITIONS:
        raise ContractError("current state is unsupported")
    if not isinstance(requested, str):
        raise ContractError("requested state is unsupported")
    if requested not in TRANSITIONS[current]:
        raise ContractError(f"state transition {current} -> {requested} is forbidden")
    return requested


def cleanup_plan(manifest: object) -> list[dict]:
    """Return a stable reverse-dependency delete plan for exact manifest IDs."""
    data = validate_manifest(manifest)
    if data["state"] not in {"teardown_requested", "cleaning", "incomplete"}:
        raise ContractError("cleanup is allowed only after teardown is requested")
    resources = [
        item for item in data["resources"]
        if item["type"] in DELETE_ORDER and item.get("status") != "deleted"
    ]
    return sorted(
        resources,
        key=lambda item: (
            -DELETE_ORDER[item["type"]],
            item["type"],
            item["id"],
        ),
    )


def control_cleanup_plan(manifest: object) -> list[dict]:
    """Plan operator reclamation after the workload inventory audit passes."""
    data = validate_manifest(manifest)
    if data["state"] != "controls_reclaiming":
        raise ContractError("recovery controls may be removed only after workload audit")
    if any(
        item.get("status") != "deleted"
        for item in data["resources"]
    ):
        raise ContractError("all workload resources must be absent before control cleanup")
    audit = data.get("independent_audit")
    if (
        not isinstance(audit, dict)
        or audit.get("result") != "passed"
        or audit.get("complete") is not True
        or audit.get("phase") != "workload"
        or audit.get("resources_absent") is not True
    ):
        raise ContractError("recovery controls require a complete independent workload audit")
    return sorted(
        (
            item for item in data["controls"]
            if item.get("status") != "deleted"
        ),
        key=lambda item: (
            -CONTROL_DELETE_ORDER[item["type"]],
            item["type"],
            item["id"],
        ),
    )
