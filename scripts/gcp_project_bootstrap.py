"""Resumable, ledger-backed GCP project activation stages.

The caller supplies an explicit project adapter and a fresh finance/budget
preflight. This module never discovers credentials. It advances only after
the exact project, billing, and API operation results are durable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from gcp_project_lifecycle_adapter import (
    BILLING_TYPE,
    PROJECT_TYPE,
    SERVICE_TYPE,
    ProjectLifecycleError,
)
from recovery_controller import OperationExecution, RecoveryController
from run_contract import MAX_OPERATION_ATTEMPTS
from run_ledger import LedgerSnapshot, RunLedger


class ProjectBootstrapError(RuntimeError):
    """Activation request failed a durable run or safety precondition."""


class FinancePreflight(Protocol):
    """Fresh read-only review of credit, the project budget, and alert controls."""

    def verify(self, manifest: dict, *, observed_at: datetime) -> None: ...

    def verify_project_budget(
        self, manifest: dict, *, observed_at: datetime
    ) -> dict | None: ...


@dataclass(frozen=True)
class ProjectBootstrapResult:
    snapshot: LedgerSnapshot
    status: str
    last_operation: OperationExecution | None = None


class GCPProjectBootstrapper:
    """Create one exact project, attach its reviewed billing account, enable APIs."""

    def __init__(
        self,
        ledger: RunLedger,
        provider,
        finance_preflight: FinancePreflight,
        *,
        clock=None,
    ):
        if finance_preflight is None or not callable(
            getattr(finance_preflight, "verify", None)
        ) or not callable(getattr(finance_preflight, "verify_project_budget", None)):
            raise ValueError("a fresh GCP finance preflight is required")
        self.ledger = ledger
        self.provider = provider
        self.finance_preflight = finance_preflight
        self.controller = RecoveryController(ledger, provider, clock=clock)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        run_id: str,
        *,
        confirm_project_id: str,
        confirm_intent_sha256: str,
        retry_failed: bool = False,
    ) -> ProjectBootstrapResult:
        """Resume one activation attempt, stopping on unresolved provider work."""
        if type(retry_failed) is not bool:
            raise ProjectBootstrapError("retry_failed must be an explicit boolean")
        snapshot = self.ledger.read(run_id)
        if confirm_project_id != snapshot.manifest["scope"]["project_id"]:
            raise ProjectBootstrapError("confirmation does not match the exact run project")
        if confirm_intent_sha256 != snapshot.metadata["intent_sha256"]:
            raise ProjectBootstrapError("confirmation does not match the registered run intent")
        if snapshot.manifest["state"] not in {
            "prepared", "project_create_requested", "project_created",
            "billing_link_requested", "project_linked", "budget_verification",
            "api_enablement",
        }:
            raise ProjectBootstrapError("run is not in a project activation state")

        snapshot = self.ledger.acquire_lease(
            run_id,
            expected_generation=snapshot.generation,
            owner="launcher",
            now=self.clock(),
            ttl=timedelta(minutes=5),
        )
        last_operation = None
        try:
            self.finance_preflight.verify(
                snapshot.manifest, observed_at=self.clock()
            )
            while True:
                state = snapshot.manifest["state"]
                if state == "prepared":
                    snapshot = self._heartbeat(snapshot)
                    project = self.provider.inspect_resource(
                        PROJECT_TYPE,
                        f"projects/{snapshot.manifest['scope']['project_id']}",
                    )
                    if project.state != "absent":
                        return ProjectBootstrapResult(
                            snapshot, "project_preflight_not_absent"
                        )
                    snapshot = self.ledger.advance_project_lifecycle(
                        run_id,
                        expected_generation=snapshot.generation,
                        lease_owner="launcher",
                        state="project_create_requested",
                        now=self.clock(),
                    )
                    continue

                if state == "project_create_requested":
                    execution = self._execute(
                        snapshot,
                        action="create",
                        resource_type=PROJECT_TYPE,
                        resource_id=f"projects/{snapshot.manifest['scope']['project_id']}",
                        request={
                            "project_id": snapshot.manifest["scope"]["project_id"],
                            "display_name": run_id,
                            "parent": snapshot.manifest["scope"]["parent"],
                            "labels": {"lab_run_id": run_id},
                        },
                        retry_failed=retry_failed,
                    )
                    if isinstance(execution, ProjectBootstrapResult):
                        return execution
                    last_operation = execution
                    snapshot = execution.snapshot
                    if execution.state not in {"succeeded", "reconciled_present"}:
                        return ProjectBootstrapResult(
                            snapshot, self._status(execution.state), execution
                        )
                    snapshot = self.ledger.advance_project_lifecycle(
                        run_id,
                        expected_generation=snapshot.generation,
                        lease_owner="launcher",
                        state="project_created",
                        now=self.clock(),
                    )
                    continue

                if state == "project_created":
                    snapshot = self.ledger.advance_project_lifecycle(
                        run_id,
                        expected_generation=snapshot.generation,
                        lease_owner="launcher",
                        state="billing_link_requested",
                        now=self.clock(),
                    )
                    continue

                if state == "billing_link_requested":
                    project_id = snapshot.manifest["scope"]["project_id"]
                    execution = self._execute(
                        snapshot,
                        action="create",
                        resource_type=BILLING_TYPE,
                        resource_id=f"projects/{project_id}",
                        request={
                            "billing_account_name": snapshot.manifest["finance"][
                                "billing_account_id"
                            ]
                        },
                        retry_failed=retry_failed,
                    )
                    if isinstance(execution, ProjectBootstrapResult):
                        return execution
                    last_operation = execution
                    snapshot = execution.snapshot
                    if execution.state not in {"succeeded", "reconciled_present"}:
                        return ProjectBootstrapResult(
                            snapshot, self._status(execution.state), execution
                        )
                    snapshot = self.ledger.advance_project_lifecycle(
                        run_id,
                        expected_generation=snapshot.generation,
                        lease_owner="launcher",
                        state="project_linked",
                        now=self.clock(),
                    )
                    continue

                if state == "project_linked":
                    snapshot = self.ledger.advance_project_lifecycle(
                        run_id,
                        expected_generation=snapshot.generation,
                        lease_owner="launcher",
                        state="budget_verification",
                        now=self.clock(),
                    )
                    continue

                if state == "budget_verification":
                    proof = self.finance_preflight.verify_project_budget(
                        snapshot.manifest, observed_at=self.clock()
                    )
                    if proof is None:
                        return ProjectBootstrapResult(
                            snapshot, "awaiting_project_budget"
                        )
                    snapshot = self.ledger.record_budget_verification(
                        run_id,
                        expected_generation=snapshot.generation,
                        lease_owner="launcher",
                        **proof,
                        now=self.clock(),
                    )
                    snapshot = self.ledger.advance_project_lifecycle(
                        run_id,
                        expected_generation=snapshot.generation,
                        lease_owner="launcher",
                        state="api_enablement",
                        now=self.clock(),
                    )
                    continue

                project_id = snapshot.manifest["scope"]["project_id"]
                for service in snapshot.manifest["scope"]["enabled_api_services"]:
                    resource_id = f"projects/{project_id}/services/{service}"
                    latest = self._latest_result(
                        snapshot, SERVICE_TYPE, resource_id
                    )
                    if latest in {"succeeded", "reconciled_present"}:
                        continue
                    execution = self._execute(
                        snapshot,
                        action="create",
                        resource_type=SERVICE_TYPE,
                        resource_id=resource_id,
                        request={},
                        retry_failed=retry_failed,
                    )
                    if isinstance(execution, ProjectBootstrapResult):
                        return execution
                    last_operation = execution
                    snapshot = execution.snapshot
                    if execution.state not in {"succeeded", "reconciled_present"}:
                        return ProjectBootstrapResult(
                            snapshot, self._status(execution.state), execution
                        )

                snapshot = self.ledger.advance_project_lifecycle(
                    run_id,
                    expected_generation=snapshot.generation,
                    lease_owner="launcher",
                    state="provisioning",
                    now=self.clock(),
                )
                return ProjectBootstrapResult(
                    snapshot, "ready_for_workload_provisioning", last_operation
                )
        finally:
            latest = self.ledger.read(run_id)
            if latest.metadata["lease"]["owner"] == "launcher":
                self.ledger.release_lease(
                    run_id,
                    expected_generation=latest.generation,
                    owner="launcher",
                    now=self.clock(),
                )

    def _execute(
        self,
        snapshot: LedgerSnapshot,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        request: dict,
        retry_failed: bool,
    ) -> OperationExecution | ProjectBootstrapResult:
        prior = [
            event for event in snapshot.manifest["operations"]
            if event["kind"] == "intent"
            and event["action"] == action
            and event["resource_type"] == resource_type
            and event["resource_id"] == resource_id
        ]
        results = {
            event["operation_id"]: event
            for event in snapshot.manifest["operations"]
            if event["kind"] == "result"
        }
        attempt = 1
        if prior:
            latest = prior[-1]
            result = results.get(latest["operation_id"])
            if result is None or result["outcome"] in {
                "succeeded", "reconciled_present"
            }:
                attempt = latest["attempt"]
            elif not retry_failed:
                return ProjectBootstrapResult(snapshot, "failed_operation_requires_review")
            else:
                attempt = latest["attempt"] + 1
        if attempt > MAX_OPERATION_ATTEMPTS:
            return ProjectBootstrapResult(snapshot, "provider_attempt_limit_reached")
        snapshot = self._heartbeat(snapshot)
        try:
            return self.controller.execute(
                snapshot.run_id,
                lease_owner="launcher",
                attempt=attempt,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                request=request,
            )
        except (ConnectionError, TimeoutError, ProjectLifecycleError):
            # The exact intent is already durable. A later invocation will
            # inspect its resource ID and reconcile rather than resubmit.
            latest = self.ledger.read(snapshot.run_id)
            return ProjectBootstrapResult(latest, "provider_response_uncertain")

    def _heartbeat(self, snapshot: LedgerSnapshot) -> LedgerSnapshot:
        return self.ledger.heartbeat(
            snapshot.run_id,
            expected_generation=snapshot.generation,
            owner="launcher",
            now=self.clock(),
            ttl=timedelta(minutes=5),
        )

    @staticmethod
    def _latest_result(snapshot: LedgerSnapshot, resource_type: str, resource_id: str):
        results = {
            event["operation_id"]: event
            for event in snapshot.manifest["operations"]
            if event["kind"] == "result"
        }
        for event in reversed(snapshot.manifest["operations"]):
            if (
                event["kind"] == "intent"
                and event["action"] == "create"
                and event["resource_type"] == resource_type
                and event["resource_id"] == resource_id
            ):
                result = results.get(event["operation_id"])
                return None if result is None else result["outcome"]
        return None

    @staticmethod
    def _status(state: str) -> str:
        return {
            "running": "waiting_for_provider_operation",
            "unresolved": "provider_reconciliation_required",
            "failed": "provider_operation_failed",
        }.get(state, "project_activation_stopped")
