"""Fail-closed Cloud Run Job entrypoint for post-audit project retirement.

This job handles only the final project-retirement phase. Workload cleanup and
independent workload/closure audits remain separate duties. All mutable target
values are checked against the durable manifest before a lease is taken or a
provider mutation is submitted.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
from typing import Callable, Mapping

from project_reclaimer_runtime import (
    ProjectReclaimerConfig,
    ProjectReclaimerRuntime,
    build_cloud_run_project_reclaimer_runtime,
)
from run_contract import ContractError, validate_manifest


_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_RUN_ID = re.compile(r"^lab-[a-z0-9](?:[a-z0-9-]{0,17}[a-z0-9])?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$")
_PARENT = re.compile(r"^(?:folders|organizations)/[0-9]{1,20}$")
_BILLING = re.compile(r"^billingAccounts/[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$")
_AUDIENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_NUMERIC_ID = re.compile(r"^[0-9]{1,30}$")
_EXECUTION_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ReaperEntrypointError(RuntimeError):
    """The scheduled job configuration or current run state is unsafe."""


@dataclass(frozen=True)
class ReaperJobConfig:
    run_id: str
    project_id: str
    recovery_project_id: str
    parent: str
    billing_account_name: str
    ledger_bucket_name: str
    enabled_services: tuple[str, ...]
    expected_email: str
    service_account_numeric_id: str
    identity_audience: str

    @property
    def expected_auditor_id(self) -> str:
        return f"google-service-account-{self.service_account_numeric_id}"

    def runtime_config(self) -> ProjectReclaimerConfig:
        return ProjectReclaimerConfig(
            ledger_bucket_name=self.ledger_bucket_name,
            ledger_project_id=self.recovery_project_id,
            project_id=self.project_id,
            run_id=self.run_id,
            parent=self.parent,
            billing_account_name=self.billing_account_name,
            enabled_services=self.enabled_services,
            expected_email=self.expected_email,
            expected_auditor_id=self.expected_auditor_id,
            identity_audience=self.identity_audience,
        )


def parse_job_config(values: Mapping[str, str]) -> ReaperJobConfig:
    """Parse the fixed per-run Job environment; no credential discovery occurs."""
    required = {
        "PORTFOLIO_RUN_ID",
        "PORTFOLIO_PROJECT_ID",
        "PORTFOLIO_RECOVERY_PROJECT_ID",
        "PORTFOLIO_RUN_PARENT",
        "PORTFOLIO_BILLING_ACCOUNT",
        "PORTFOLIO_LEDGER_BUCKET",
        "PORTFOLIO_ENABLED_SERVICES",
        "PORTFOLIO_REAPER_EMAIL",
        "PORTFOLIO_REAPER_SERVICE_ACCOUNT_ID",
        "PORTFOLIO_IDENTITY_AUDIENCE",
    }
    missing = sorted(required - set(values))
    if missing:
        raise ReaperEntrypointError("required per-run Job configuration is missing")

    try:
        services = json.loads(values["PORTFOLIO_ENABLED_SERVICES"])
    except (TypeError, json.JSONDecodeError):
        raise ReaperEntrypointError("enabled service configuration is malformed") from None
    if (
        not isinstance(services, list)
        or not services
        or any(not isinstance(item, str) for item in services)
        or services != sorted(set(services))
    ):
        raise ReaperEntrypointError("enabled services must be a sorted unique JSON list")

    config = ReaperJobConfig(
        run_id=values["PORTFOLIO_RUN_ID"],
        project_id=values["PORTFOLIO_PROJECT_ID"],
        recovery_project_id=values["PORTFOLIO_RECOVERY_PROJECT_ID"],
        parent=values["PORTFOLIO_RUN_PARENT"],
        billing_account_name=values["PORTFOLIO_BILLING_ACCOUNT"],
        ledger_bucket_name=values["PORTFOLIO_LEDGER_BUCKET"],
        enabled_services=tuple(services),
        expected_email=values["PORTFOLIO_REAPER_EMAIL"],
        service_account_numeric_id=values["PORTFOLIO_REAPER_SERVICE_ACCOUNT_ID"],
        identity_audience=values["PORTFOLIO_IDENTITY_AUDIENCE"],
    )
    if _RUN_ID.fullmatch(config.run_id) is None:
        raise ReaperEntrypointError("per-run Job has an invalid run ID")
    if (
        _PROJECT_ID.fullmatch(config.project_id) is None
        or _PROJECT_ID.fullmatch(config.recovery_project_id) is None
        or config.project_id == config.recovery_project_id
    ):
        raise ReaperEntrypointError("per-run Job project scope is invalid")
    if _PARENT.fullmatch(config.parent) is None:
        raise ReaperEntrypointError("per-run Job parent is invalid")
    if _BILLING.fullmatch(config.billing_account_name) is None:
        raise ReaperEntrypointError("per-run Job billing account is invalid")
    if _BUCKET.fullmatch(config.ledger_bucket_name) is None:
        raise ReaperEntrypointError("per-run Job ledger bucket is invalid")
    if _EMAIL.fullmatch(config.expected_email) is None:
        raise ReaperEntrypointError("per-run Job service account email is invalid")
    if _NUMERIC_ID.fullmatch(config.service_account_numeric_id) is None:
        raise ReaperEntrypointError("per-run Job service account ID is invalid")
    if _AUDIENCE.fullmatch(config.identity_audience) is None:
        raise ReaperEntrypointError("per-run Job identity audience is invalid")
    return config


def _verify_manifest_binding(manifest: object, config: ReaperJobConfig) -> dict:
    try:
        run = validate_manifest(manifest)
    except ContractError as exc:
        raise ReaperEntrypointError("durable run manifest violates its contract") from exc
    if (
        run["run_id"] != config.run_id
        or run["scope"]["project_id"] != config.project_id
        or run["scope"]["recovery_project_id"] != config.recovery_project_id
        or run["scope"]["ledger_bucket_name"] != config.ledger_bucket_name
        or run["scope"]["parent"] != config.parent
        or tuple(run["scope"]["enabled_api_services"]) != config.enabled_services
        or run["finance"]["billing_account_id"] != config.billing_account_name
        or run["cleanup"]["service_account"] != config.expected_email
        or run["cleanup"]["identity_audience"] != config.identity_audience
    ):
        raise ReaperEntrypointError(
            "per-run Job configuration does not match the durable run manifest"
        )
    expected_control = (
        f"projects/{config.recovery_project_id}/serviceAccounts/"
        f"{config.run_id}-reaper@{config.recovery_project_id}.iam.gserviceaccount.com"
    )
    if not any(
        control["type"] == "gcp.iam.service_account"
        and control["id"] == expected_control
        for control in run["controls"]
    ):
        raise ReaperEntrypointError("durable manifest does not declare this run's reaper")
    return run


def _next_attempt(manifest: dict, action: str, resource_type: str, resource_id: str) -> int:
    intents = [
        event
        for event in manifest["operations"]
        if event["kind"] == "intent"
        and event["action"] == action
        and event["resource_type"] == resource_type
        and event["resource_id"] == resource_id
        and event["phase"] == "project_retiring"
    ]
    results = {
        event["operation_id"]: event
        for event in manifest["operations"]
        if event["kind"] == "result"
    }
    if intents:
        latest = intents[-1]
        if latest["operation_id"] not in results:
            return latest["attempt"]
        if results[latest["operation_id"]]["outcome"] in {
            "succeeded", "reconciled_absent"
        }:
            return latest["attempt"]
        return latest["attempt"] + 1
    return 1


def execute_project_retirement(
    runtime: ProjectReclaimerRuntime,
    config: ReaperJobConfig,
    *,
    execution_id: str,
    now: datetime | None = None,
) -> str:
    """Run only the manifest-authorized billing unlink and project deletion."""
    at = now or datetime.now(timezone.utc)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ReaperEntrypointError("job clock must include a timezone")
    if not isinstance(execution_id, str) or _EXECUTION_ID.fullmatch(execution_id) is None:
        raise ReaperEntrypointError("Cloud Run execution ID is missing or invalid")
    snapshot = runtime.ledger.read(config.run_id)
    run = _verify_manifest_binding(snapshot.manifest, config)
    prior_retirement = any(
        event["kind"] == "intent"
        and event["phase"] == "project_retiring"
        and event["action"] == "delete"
        and event["resource_type"] in {
            "gcp.billing.project_billing_info",
            "gcp.resourcemanager.project",
        }
        for event in run["operations"]
    )
    passed_workload_audit = any(
        receipt["phase"] == "workload"
        and receipt["result"] == "passed"
        and receipt["complete"] is True
        and receipt["checks"].get("resources_absent") is True
        for receipt in run.get("audit_receipts", [])
    )
    resumable_incomplete = (
        run["state"] == "incomplete"
        and prior_retirement
        and passed_workload_audit
        and all(resource["status"] == "deleted" for resource in run["resources"])
    )
    if run["state"] != "project_retiring" and not resumable_incomplete:
        return "waiting_for_independent_audit_or_prior_phase"
    deadline = datetime.fromisoformat(run["deadline_at"].replace("Z", "+00:00"))
    if at.astimezone(timezone.utc) < deadline:
        return "before_run_deadline"

    lease_owner = f"janitor:{config.expected_auditor_id}:{execution_id}"
    leased = runtime.ledger.acquire_lease(
        config.run_id,
        expected_generation=snapshot.generation,
        owner=lease_owner,
        now=at,
    )
    if resumable_incomplete:
        candidate = deepcopy(leased.manifest)
        candidate["state"] = "project_retiring"
        leased = runtime.ledger.replace(
            config.run_id,
            expected_generation=leased.generation,
            lease_owner=lease_owner,
            manifest=candidate,
            now=at,
        )
    project_resource = f"projects/{config.project_id}"

    billing_attempt = _next_attempt(
        leased.manifest,
        "delete",
        "gcp.billing.project_billing_info",
        project_resource,
    )
    billing_result = runtime.controller.execute(
        config.run_id,
        lease_owner=lease_owner,
        attempt=billing_attempt,
        action="delete",
        resource_type="gcp.billing.project_billing_info",
        resource_id=project_resource,
        request={"billing_account_name": ""},
    )
    if billing_result.state not in {"succeeded", "reconciled_absent"}:
        return "billing_unlink_pending_or_incomplete"

    latest = runtime.ledger.read(config.run_id)
    project_attempt = _next_attempt(
        latest.manifest,
        "delete",
        "gcp.resourcemanager.project",
        project_resource,
    )
    project_result = runtime.controller.execute(
        config.run_id,
        lease_owner=lease_owner,
        attempt=project_attempt,
        action="delete",
        resource_type="gcp.resourcemanager.project",
        resource_id=project_resource,
        request={},
    )
    if project_result.state not in {"succeeded", "reconciled_absent"}:
        return "project_deletion_pending_or_incomplete"
    return "project_absence_reconciled_controls_audit_required"


def build_runtime(config: ReaperJobConfig) -> ProjectReclaimerRuntime:
    """Construct the Cloud Run runtime with explicit metadata credentials."""
    try:
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise ReaperEntrypointError("google-auth transport dependency is unavailable") from exc
    from gcp_cleanup_adapter import UrllibTransport

    return build_cloud_run_project_reclaimer_runtime(
        config.runtime_config(),
        transport=UrllibTransport(),
        request=Request(),
    )


def main(
    environ: Mapping[str, str] | None = None,
    *,
    runtime_factory: Callable[[ReaperJobConfig], ProjectReclaimerRuntime] = build_runtime,
    now: datetime | None = None,
) -> str:
    """Cloud Run Job entry; logs a bounded outcome and propagates safe errors."""
    config = parse_job_config(os.environ if environ is None else environ)
    values = os.environ if environ is None else environ
    execution_id = values.get("CLOUD_RUN_EXECUTION", "")
    if _EXECUTION_ID.fullmatch(execution_id) is None:
        raise ReaperEntrypointError("Cloud Run execution ID is missing or invalid")
    runtime = runtime_factory(config)
    outcome = execute_project_retirement(
        runtime, config, execution_id=execution_id, now=now
    )
    print(json.dumps({"schema": "portfolio.reaper.v1", "run_id": config.run_id,
                      "outcome": outcome}, sort_keys=True))
    return outcome


if __name__ == "__main__":
    main()
