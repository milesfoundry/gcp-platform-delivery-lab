"""Credential-free orchestration for durable GCP provider operations.

This module coordinates the run ledger with an injected provider adapter. It
does not construct credentials, make Google API calls, or store request bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Protocol

from run_contract import ContractError
from run_ledger import LedgerSnapshot, RunLedger


FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
RECEIPT_STATES = frozenset(
    {"running", "succeeded", "failed", "present", "absent", "unknown"}
)


@dataclass(frozen=True)
class ProviderReceipt:
    """A minimized status; running is nonterminal, succeeded/failed are final.

    `present` and `absent` describe exact-resource observations, while `unknown`
    deliberately leaves the durable intent unresolved. Never include a raw API
    response in this receipt.
    """

    state: str
    response_sha256: str
    operation_name: str | None = None


@dataclass(frozen=True)
class OperationExecution:
    """The durable ledger snapshot and whether this attempt is terminal."""

    snapshot: LedgerSnapshot
    operation_id: str
    state: str


class ProviderAdapter(Protocol):
    """Narrow adapter contract; real GCP implementations remain unqualified."""

    def submit(
        self,
        action: str,
        resource_type: str,
        resource_id: str,
        request: dict,
    ) -> ProviderReceipt: ...

    def inspect_resource(self, resource_type: str, resource_id: str) -> ProviderReceipt: ...

    def poll_operation(self, operation_name: str) -> ProviderReceipt: ...


def request_fingerprint(request: dict) -> str:
    """Hash a canonical JSON request without persisting its contents."""
    if not isinstance(request, dict):
        raise ContractError("provider request must be a JSON object")
    try:
        canonical = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError("provider request must be finite canonical JSON") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RecoveryController:
    """Persist, submit once, and reconcile exact-ID provider operations."""

    def __init__(self, ledger: RunLedger, provider: ProviderAdapter, *, clock=None):
        self.ledger = ledger
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(
        self,
        run_id: str,
        *,
        lease_owner: str,
        attempt: int,
        action: str,
        resource_type: str,
        resource_id: str,
        request: dict,
    ) -> OperationExecution:
        """Submit a new operation or reconcile its existing exact intent.

        Existing intents are never resubmitted. If the adapter cannot prove a
        terminal result from the exact resource or provider operation, the
        durable intent remains unresolved for a later retry or operator review.
        """
        request_sha256 = request_fingerprint(request)
        before = self.ledger.read(run_id)
        existing_intent = next(
            (
                event
                for event in before.manifest["operations"]
                if event["kind"] == "intent"
                and event["attempt"] == attempt
                and event["action"] == action
                and event["resource_type"] == resource_type
                and event["resource_id"] == resource_id
            ),
            None,
        )
        snapshot = self.ledger.record_operation_intent(
            run_id,
            expected_generation=before.generation,
            lease_owner=lease_owner,
            attempt=attempt,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_sha256=request_sha256,
            now=self.clock(),
        )
        intent = next(
            event
            for event in snapshot.manifest["operations"]
            if event["kind"] == "intent"
            and event["attempt"] == attempt
            and event["action"] == action
            and event["resource_type"] == resource_type
            and event["resource_id"] == resource_id
            and event["request_sha256"] == request_sha256
        )
        operation_id = intent["operation_id"]
        completed = next(
            (
                event
                for event in snapshot.manifest["operations"]
                if event["kind"] == "result" and event["operation_id"] == operation_id
            ),
            None,
        )
        if completed is not None:
            return OperationExecution(snapshot, operation_id, completed["outcome"])

        prior_progress = next(
            (
                event
                for event in reversed(snapshot.manifest["operations"])
                if event["kind"] == "progress" and event["operation_id"] == operation_id
            ),
            None,
        )
        if existing_intent is None:
            checked = self.ledger.assert_active_lease(
                run_id,
                expected_generation=snapshot.generation,
                owner=lease_owner,
                now=self.clock(),
            )
            receipt = self.provider.submit(
                action, resource_type, resource_id, request
            )
            snapshot = checked
        elif prior_progress is not None:
            receipt = self.provider.poll_operation(
                prior_progress["provider_operation_name"]
            )
        else:
            receipt = self.provider.inspect_resource(resource_type, resource_id)

        self._validate_receipt(receipt)
        if receipt.operation_name is None and prior_progress is not None:
            receipt = ProviderReceipt(
                receipt.state,
                receipt.response_sha256,
                prior_progress["provider_operation_name"],
            )
        elif (
            prior_progress is not None
            and receipt.operation_name != prior_progress["provider_operation_name"]
        ):
            raise ContractError("provider operation name changed during reconciliation")

        at = self.clock()
        if receipt.state == "running":
            if not receipt.operation_name:
                raise ContractError("running provider operation requires an operation name")
            snapshot = self.ledger.record_operation_progress(
                run_id,
                expected_generation=snapshot.generation,
                lease_owner=lease_owner,
                operation_id_value=operation_id,
                provider_operation_name=receipt.operation_name,
                response_sha256=receipt.response_sha256,
                now=at,
            )
            return OperationExecution(snapshot, operation_id, "running")

        outcome = None
        if receipt.state in {"succeeded", "failed"}:
            outcome = receipt.state
        elif action == "create" and receipt.state == "present":
            outcome = "reconciled_present"
        elif action == "delete" and receipt.state == "absent":
            outcome = "reconciled_absent"
        if outcome is None:
            return OperationExecution(snapshot, operation_id, "unresolved")

        snapshot = self.ledger.record_operation_result(
            run_id,
            expected_generation=snapshot.generation,
            lease_owner=lease_owner,
            operation_id_value=operation_id,
            outcome=outcome,
            provider_operation_name=receipt.operation_name,
            response_sha256=receipt.response_sha256,
            now=at,
        )
        return OperationExecution(snapshot, operation_id, outcome)

    @staticmethod
    def _validate_receipt(receipt: ProviderReceipt) -> None:
        if not isinstance(receipt, ProviderReceipt):
            raise ContractError("provider adapter must return a minimized ProviderReceipt")
        if not isinstance(receipt.state, str) or receipt.state not in RECEIPT_STATES:
            raise ContractError("provider receipt state is unsupported")
        if (
            not isinstance(receipt.response_sha256, str)
            or not FINGERPRINT.fullmatch(receipt.response_sha256)
        ):
            raise ContractError("provider receipt must contain a SHA-256 fingerprint")
        if receipt.operation_name is not None and (
            not isinstance(receipt.operation_name, str)
            or not receipt.operation_name
        ):
            raise ContractError("provider operation name must be a non-empty string")
