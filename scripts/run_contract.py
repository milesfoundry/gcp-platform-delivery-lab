"""Credential-free validation for GCP lab run manifests and cleanup plans.

This module validates intent and produces a stable cleanup order. It does not
authenticate, call Google Cloud APIs, persist a ledger, or delete resources.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re


SCHEMA = "portfolio.gcp-run.v1"
MAX_RUN_LIFETIME = timedelta(minutes=60)
RUN_ID = re.compile(r"^lab-[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?$")
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST = re.compile(r"^ghcr\.io/[a-z0-9-]+/[a-z0-9-]+@sha256:[0-9a-f]{64}$")
RESOURCE_URI = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:/._@+=,-]{0,1023}$")
GENERATION = re.compile(r"^[1-9][0-9]*$")

REGION = "europe-west2"
ZONES = frozenset(
    {"europe-west2-a", "europe-west2-b", "europe-west2-c"}
)

STATES = frozenset(
    {
        "prepared",
        "provisioning",
        "ready",
        "teardown_requested",
        "cleaning",
        "auditing",
        "closed",
        "incomplete",
    }
)

TRANSITIONS = {
    "prepared": {"provisioning", "teardown_requested", "incomplete"},
    "provisioning": {"ready", "teardown_requested", "incomplete"},
    "ready": {"teardown_requested", "incomplete"},
    "teardown_requested": {"cleaning", "incomplete"},
    "cleaning": {"auditing", "incomplete"},
    "auditing": {"closed", "cleaning", "incomplete"},
    "incomplete": {"teardown_requested", "cleaning", "auditing"},
    "closed": set(),
}

# Larger values are deleted earlier. Bootstrap resources and the durable ledger
# are deliberately absent: a run manifest must never authorize their deletion.
DELETE_ORDER = {
    "gcp.storage.object": 200,
    "gcp.pubsub.subscription": 190,
    "gcp.pubsub.topic": 180,
    "gcp.gkehub.feature_membership": 170,
    "gcp.gkehub.membership": 160,
    "gcp.gke.node_pool": 150,
    "gcp.gke.cluster": 140,
    "gcp.compute.router_nat": 110,
    "gcp.compute.router": 100,
    "gcp.compute.subnetwork": 90,
    "gcp.compute.network": 80,
}

# The per-run Scheduler trigger remains alive through workload cleanup/audit.
# The generic Cloud Run reaper Job is persistent bootstrap infrastructure and is
# never accepted in a run manifest.
CONTROL_DELETE_ORDER = {
    "gcp.scheduler.job": 20,
}

OWNERSHIP_PROOFS = frozenset({"run_label", "run_name", "manifest_record"})
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
            "finance", "cleanup", "state", "resources", "independent_audit",
        },
        "intent",
    )
    if data.get("schema") != SCHEMA:
        raise ContractError("schema is unsupported")

    run_id = _string(data.get("run_id"), "run_id")
    if not RUN_ID.fullmatch(run_id):
        raise ContractError("run_id must be a bounded lab-prefixed identifier")

    scope = _mapping(data.get("scope"), "scope")
    _only_keys(scope, {"project_id", "region", "zones"}, "scope")
    project_id = _string(scope.get("project_id"), "scope.project_id")
    if not PROJECT_ID.fullmatch(project_id):
        raise ContractError("scope.project_id is invalid")
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

    finance = _mapping(data.get("finance"), "finance")
    _only_keys(
        finance,
        {
            "currency", "gross_estimate_gbp", "net_cash_exposure_gbp",
            "available_credit_gbp", "credit_observed_at", "credit_expires_at",
            "monthly_alert_threshold_gbp", "alerts_verified",
        },
        "finance",
    )
    if finance.get("currency") != "GBP":
        raise ContractError("finance.currency must be GBP")
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
        {"service_account", "identity_verified", "independent_recovery_verified"},
        "cleanup",
    )
    cleanup_account = _string(
        cleanup.get("service_account"), "cleanup.service_account"
    )
    if not cleanup_account.endswith(f"@{project_id}.iam.gserviceaccount.com"):
        raise ContractError("cleanup.service_account must belong to the approved project")
    if cleanup.get("identity_verified") is not True:
        raise ContractError("cleanup.identity_verified must be true")
    if cleanup.get("independent_recovery_verified") is not True:
        raise ContractError("independent cleanup recovery must be verified")

    if new_run:
        if data.get("state") != "prepared":
            raise ContractError("new intent must start in prepared state")
        if data.get("resources") != []:
            raise ContractError("new intent must have an empty resource list")
    return data


def validate_manifest(manifest: object) -> dict:
    """Validate a persisted manifest before changing it or acting on resources."""
    data = _mapping(manifest, "manifest")
    validate_intent(data, new_run=False)
    if not isinstance(data.get("state"), str) or data["state"] not in STATES:
        raise ContractError("state is unsupported")

    resources = data.get("resources")
    if not isinstance(resources, list):
        raise ContractError("resources must be a list")
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
        if resource_type not in DELETE_ORDER and resource_type not in CONTROL_DELETE_ORDER:
            raise ContractError(f"resource type is not deletable: {resource_type}")
        resource_id = _string(entry.get("id"), f"resources[{index}].id")
        project_segment = re.compile(rf"(?:^|/)projects/{re.escape(project_id)}/")
        if (
            not RESOURCE_URI.fullmatch(resource_id)
            or not project_segment.search(resource_id)
        ):
            raise ContractError("resource id must be a canonical URI in this project")
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
        if run_id not in resource_id:
            raise ContractError("run-scoped resource id must include the run id")
        if (
            not isinstance(entry.get("status"), str)
            or entry["status"] not in {"expected", "present", "deleted"}
        ):
            raise ContractError("resource status is unsupported")
        attempts = entry.get("delete_attempts", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ContractError("delete_attempts must be a non-negative integer")

    audit = None
    if "independent_audit" in data:
        audit = _mapping(data["independent_audit"], "independent_audit")
        _only_keys(audit, {"result", "complete"}, "independent_audit")
        if audit.get("result") not in {"passed", "failed", "incomplete"}:
            raise ContractError("independent audit result is unsupported")
        if not isinstance(audit.get("complete"), bool):
            raise ContractError("independent audit complete must be boolean")

    if data["state"] == "closed":
        if any(resource["status"] != "deleted" for resource in resources):
            raise ContractError("closed manifest must have no live run resources")
        if audit is None or audit.get("result") != "passed":
            raise ContractError("closed manifest requires a passed independent audit")
        if audit.get("complete") is not True:
            raise ContractError("closed manifest requires complete audit coverage")
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
    """Plan reaper removal only after the workload inventory audit passes."""
    data = validate_manifest(manifest)
    if data["state"] != "auditing":
        raise ContractError("recovery controls may be removed only after workload audit")
    if any(
        item.get("status") != "deleted"
        for item in data["resources"]
        if item.get("type") not in CONTROL_DELETE_ORDER
    ):
        raise ContractError("all workload resources must be absent before control cleanup")
    audit = data.get("independent_audit")
    if (
        not isinstance(audit, dict)
        or audit.get("result") != "passed"
        or audit.get("complete") is not True
    ):
        raise ContractError("recovery controls require a complete independent workload audit")
    return sorted(
        (
            item for item in data["resources"]
            if item.get("type") in CONTROL_DELETE_ORDER
            and item.get("status") != "deleted"
        ),
        key=lambda item: (
            -CONTROL_DELETE_ORDER[item["type"]],
            item["type"],
            item["id"],
        ),
    )
