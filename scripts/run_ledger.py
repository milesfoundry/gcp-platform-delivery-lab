"""Generation-conditional storage and leases for exact-path GCP run records.

The module never lists or deletes manifests and does not mutate cloud resources.
A caller must know the run ID and expected object generation for every write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Protocol

from run_contract import (
    ContractError,
    DELETE_ORDER,
    MAX_RUN_LIFETIME,
    MAX_OPERATION_ATTEMPTS,
    RUN_ID,
    _audit_summary,
    next_state,
    operation_id,
    validate_intent,
    validate_manifest,
)


LEDGER_SCHEMA = "portfolio.gcp-ledger.v1"
MAX_EVENTS = 1024
MAX_LEASE = timedelta(minutes=5)
MIN_LEASE = timedelta(seconds=15)
OWNER = re.compile(
    r"^(?:launcher|reaper|operator:[a-z0-9._-]{1,64}|auditor:"
    r"google-user-[A-Za-z0-9._~-]{1,255})$"
)
EVENT_TYPES = frozenset(
    {
        "run_created",
        "manifest_updated",
        "lease_acquired",
        "lease_reclaimed",
        "lease_heartbeat",
        "lease_released",
        "operation_intent_recorded",
        "operation_progress_recorded",
        "operation_result_recorded",
        "audit_receipt_recorded",
    }
)


class LedgerError(RuntimeError):
    """Base class for durable ledger failures."""


class LedgerNotFound(LedgerError):
    """The exact run manifest does not exist."""


class GenerationConflict(LedgerError):
    """The object changed since the caller read it."""


class LedgerConflict(LedgerError):
    """An immutable run identity or transition was changed."""


class LeaseBusy(LedgerConflict):
    """Another unexpired owner holds the run lease."""


class ObjectStore(Protocol):
    """Minimal conditional object API used by the run ledger."""

    def read(self, name: str) -> tuple[bytes, int]: ...

    def create(self, name: str, content: bytes) -> int: ...

    def replace(self, name: str, content: bytes, *, generation: int) -> int: ...


@dataclass(frozen=True)
class LedgerSnapshot:
    run_id: str
    manifest: dict
    generation: int
    metadata: dict


def object_name(run_id: str) -> str:
    """Return the one permitted object name for a syntactically valid run ID."""
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ContractError("run_id is invalid for a ledger object path")
    return f"runs/{run_id}/manifest.json"


def _canonical_json(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise LedgerError(f"record contains a duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise LedgerError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LedgerError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LedgerError("ledger time must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _make_event(sequence: int, event_type: str, at: datetime) -> dict:
    if event_type not in EVENT_TYPES:
        raise LedgerError("event type is unsupported")
    return {"sequence": sequence, "type": event_type, "at": _format_time(at)}


def _initial_envelope(manifest: dict) -> dict:
    created_at = manifest["created_at"]
    checksum = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    return {
        "schema": LEDGER_SCHEMA,
        "manifest": manifest,
        "metadata": {
            "intent_sha256": checksum,
            "event_sequence": 1,
            "events": [_make_event(1, "run_created", _parse_time(created_at, "created_at"))],
            "lease": {"owner": None, "expires_at": None},
            "last_heartbeat_at": created_at,
        },
    }


def _validate_envelope(value: object) -> tuple[dict, dict]:
    if not isinstance(value, dict) or set(value) != {"schema", "manifest", "metadata"}:
        raise LedgerError("ledger envelope fields are unsupported")
    if value.get("schema") != LEDGER_SCHEMA:
        raise LedgerError("ledger envelope schema is unsupported")
    try:
        manifest = validate_manifest(value.get("manifest"))
    except ContractError as exc:
        raise LedgerError(f"stored manifest violates the run contract: {exc}") from exc
    metadata = value.get("metadata")
    if not isinstance(metadata, dict) or set(metadata) != {
        "intent_sha256", "event_sequence", "events", "lease", "last_heartbeat_at"
    }:
        raise LedgerError("ledger metadata fields are unsupported")
    checksum = metadata["intent_sha256"]
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise LedgerError("intent_sha256 must be a SHA-256 hex digest")
    sequence = metadata["event_sequence"]
    events = metadata["events"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise LedgerError("event_sequence must be a positive integer")
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
        raise LedgerError(f"events must contain between one and {MAX_EVENTS} records")
    if sequence != len(events):
        raise LedgerError("event_sequence must equal the retained event count")
    previous_event_time = None
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict) or set(event) != {"sequence", "type", "at"}:
            raise LedgerError("event fields are unsupported")
        if isinstance(event["sequence"], bool) or event["sequence"] != index:
            raise LedgerError("event sequence must be contiguous and monotonic")
        if not isinstance(event["type"], str) or event["type"] not in EVENT_TYPES:
            raise LedgerError("event type is unsupported")
        event_time = _parse_time(event["at"], "event.at")
        if previous_event_time is not None and event_time < previous_event_time:
            raise LedgerError("event timestamps must be monotonic")
        previous_event_time = event_time
    if events[0]["type"] != "run_created":
        raise LedgerError("event history must begin with run_created")
    lease = metadata["lease"]
    if not isinstance(lease, dict) or set(lease) != {"owner", "expires_at"}:
        raise LedgerError("lease fields are unsupported")
    owner = lease["owner"]
    expires_at = lease["expires_at"]
    if owner is None:
        if expires_at is not None:
            raise LedgerError("an unowned lease cannot have an expiry")
    elif not isinstance(owner, str) or not OWNER.fullmatch(owner):
        raise LedgerError("lease owner is unsupported")
    else:
        expires = _parse_time(expires_at, "lease.expires_at")
        heartbeat = _parse_time(metadata["last_heartbeat_at"], "last_heartbeat_at")
        if expires > heartbeat + MAX_LEASE:
            raise LedgerError("lease expiry exceeds the maximum lease duration")
    heartbeat = _parse_time(metadata["last_heartbeat_at"], "last_heartbeat_at")
    if heartbeat != previous_event_time:
        raise LedgerError("last heartbeat must match the latest recorded event")
    return manifest, metadata


def _decode_envelope(content: bytes) -> tuple[dict, dict]:
    try:
        value = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError("stored ledger record is not valid UTF-8 JSON") from exc
    return _validate_envelope(value)


class RunLedger:
    """Create, read, lease, and compare-and-swap one run at a time."""

    def __init__(self, store: ObjectStore, *, clock=None):
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create(self, intent: object) -> LedgerSnapshot:
        """Create a run record once; reconcile an identical lost-response retry."""
        try:
            manifest = validate_intent(intent, now=self._clock())
        except ContractError as contract_error:
            raw_run_id = intent.get("run_id") if isinstance(intent, dict) else None
            object_name(raw_run_id)
            try:
                checksum = hashlib.sha256(_canonical_json(intent)).hexdigest()
                existing = self.read(raw_run_id)
            except LedgerNotFound:
                raise contract_error
            if existing.metadata["intent_sha256"] != checksum:
                raise LedgerConflict("run ID already has a different creation intent")
            return existing
        name = object_name(manifest["run_id"])
        envelope = _initial_envelope(manifest)
        body = _canonical_json(envelope)
        try:
            generation = self._store.create(name, body)
            return LedgerSnapshot(manifest["run_id"], manifest, generation, envelope["metadata"])
        except GenerationConflict:
            existing = self.read(manifest["run_id"])
            if existing.metadata["intent_sha256"] != envelope["metadata"]["intent_sha256"]:
                raise LedgerConflict("run ID already has a different creation intent")
            return existing

    def read(self, run_id: str) -> LedgerSnapshot:
        name = object_name(run_id)
        content, generation = self._store.read(name)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise LedgerError("object store returned an invalid generation")
        manifest, metadata = _decode_envelope(content)
        if manifest["run_id"] != run_id:
            raise LedgerError("manifest run_id does not match its exact object path")
        return LedgerSnapshot(run_id, manifest, generation, metadata)

    def assert_active_lease(
        self,
        run_id: str,
        *,
        expected_generation: int,
        owner: str,
        now: datetime | None = None,
    ) -> LedgerSnapshot:
        """Re-read the exact record and confirm its lease immediately before a call."""
        self._validate_generation(expected_generation)
        current = self.read(run_id)
        self._require_active_lease(current, owner, now or self._clock())
        if current.generation != expected_generation:
            raise GenerationConflict("manifest generation is stale")
        return current

    def _replace_envelope(
        self, run_id: str, current: LedgerSnapshot, envelope: dict
    ) -> LedgerSnapshot:
        body = _canonical_json(envelope)
        try:
            generation = self._store.replace(
                object_name(run_id), body, generation=current.generation
            )
        except GenerationConflict:
            latest = self.read(run_id)
            if _canonical_json({
                "schema": LEDGER_SCHEMA,
                "manifest": latest.manifest,
                "metadata": latest.metadata,
            }) == body:
                return latest
            raise
        return LedgerSnapshot(run_id, envelope["manifest"], generation, envelope["metadata"])

    @staticmethod
    def _append_event(metadata: dict, event_type: str, at: datetime) -> dict:
        updated = json.loads(json.dumps(metadata))
        if len(updated["events"]) >= MAX_EVENTS:
            raise LedgerConflict("event history is full; preserve the record for operator review")
        sequence = updated["event_sequence"] + 1
        updated["events"].append(_make_event(sequence, event_type, at))
        updated["event_sequence"] = sequence
        updated["last_heartbeat_at"] = _format_time(at)
        return updated

    def acquire_lease(
        self,
        run_id: str,
        *,
        expected_generation: int,
        owner: str,
        now: datetime | None = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> LedgerSnapshot:
        """Acquire an unheld/expired lease using an object-generation CAS."""
        self._validate_generation(expected_generation)
        at = now or self._clock()
        if not isinstance(owner, str) or not OWNER.fullmatch(owner):
            raise LedgerError("lease owner is unsupported")
        self._validate_ttl(ttl)
        current = self.read(run_id)
        lease = current.metadata["lease"]
        if current.generation != expected_generation:
            if lease["owner"] == owner and lease["expires_at"] is not None:
                lease_expiry = _parse_time(lease["expires_at"], "lease.expires_at")
                if lease_expiry > at:
                    return current
            raise GenerationConflict("manifest generation is stale")
        if lease["owner"] is not None:
            expires = _parse_time(lease["expires_at"], "lease.expires_at")
            if expires > at:
                if lease["owner"] == owner:
                    return current
                raise LeaseBusy("another unexpired owner holds the run lease")
        event_type = "lease_reclaimed" if lease["owner"] is not None else "lease_acquired"
        metadata = self._append_event(current.metadata, event_type, at)
        metadata["lease"] = {
            "owner": owner,
            "expires_at": _format_time(at + ttl),
        }
        envelope = {"schema": LEDGER_SCHEMA, "manifest": current.manifest, "metadata": metadata}
        try:
            return self._replace_envelope(run_id, current, envelope)
        except GenerationConflict:
            latest = self.read(run_id)
            latest_lease = latest.metadata["lease"]
            if (
                latest_lease["owner"] == owner
                and _parse_time(latest_lease["expires_at"], "lease.expires_at")
                == at + ttl
            ):
                return latest
            raise

    def heartbeat(
        self,
        run_id: str,
        *,
        expected_generation: int,
        owner: str,
        now: datetime | None = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> LedgerSnapshot:
        """Extend a held lease; an expired or foreign lease cannot be revived."""
        self._validate_generation(expected_generation)
        at = now or self._clock()
        self._validate_ttl(ttl)
        current = self.read(run_id)
        lease = current.metadata["lease"]
        if lease["owner"] != owner or lease["expires_at"] is None:
            raise LedgerConflict("heartbeat requires the current lease owner")
        expires = _parse_time(lease["expires_at"], "lease.expires_at")
        if expires <= at:
            raise LedgerConflict("expired lease must be reacquired before heartbeat")
        new_expiry = at + ttl
        if current.generation != expected_generation:
            latest_event = current.metadata["events"][-1]
            if (
                expires == new_expiry
                and latest_event["type"] == "lease_heartbeat"
                and _parse_time(latest_event["at"], "event.at") == at
            ):
                return current
            raise GenerationConflict("manifest generation is stale")
        metadata = self._append_event(current.metadata, "lease_heartbeat", at)
        metadata["lease"] = {"owner": owner, "expires_at": _format_time(new_expiry)}
        envelope = {"schema": LEDGER_SCHEMA, "manifest": current.manifest, "metadata": metadata}
        try:
            return self._replace_envelope(run_id, current, envelope)
        except GenerationConflict:
            latest = self.read(run_id)
            latest_lease = latest.metadata["lease"]
            if (
                latest_lease["owner"] == owner
                and _parse_time(latest_lease["expires_at"], "lease.expires_at") == new_expiry
            ):
                return latest
            raise

    def release_lease(
        self,
        run_id: str,
        *,
        expected_generation: int,
        owner: str,
        now: datetime | None = None,
    ) -> LedgerSnapshot:
        """Release only the caller's lease; reconcile an identical lost response."""
        self._validate_generation(expected_generation)
        at = now or self._clock()
        current = self.read(run_id)
        lease = current.metadata["lease"]
        if lease["owner"] is None and current.generation != expected_generation:
            return current
        if lease["owner"] != owner:
            raise LedgerConflict("only the current lease owner may release it")
        if current.generation != expected_generation:
            raise GenerationConflict("manifest generation is stale")
        metadata = self._append_event(current.metadata, "lease_released", at)
        metadata["lease"] = {"owner": None, "expires_at": None}
        envelope = {"schema": LEDGER_SCHEMA, "manifest": current.manifest, "metadata": metadata}
        try:
            return self._replace_envelope(run_id, current, envelope)
        except GenerationConflict:
            latest = self.read(run_id)
            if latest.metadata["lease"]["owner"] is None:
                return latest
            raise

    def replace(
        self,
        run_id: str,
        *,
        expected_generation: int,
        lease_owner: str,
        manifest: object,
        event_type: str = "manifest_updated",
        now: datetime | None = None,
    ) -> LedgerSnapshot:
        """Replace a manifest under the active lease and object generation."""
        self._validate_generation(expected_generation)
        at = now or self._clock()
        current = self.read(run_id)
        try:
            candidate = validate_manifest(manifest)
        except ContractError:
            raise
        if candidate["run_id"] != run_id:
            raise LedgerConflict("replacement cannot change the run ID")
        for key in ("schema", "scope", "created_at", "deadline_at", "source"):
            if candidate.get(key) != current.manifest.get(key):
                raise LedgerConflict(f"replacement cannot change immutable field {key}")
        for role in ("backend", "backup"):
            for key in ("bucket", "object"):
                if (
                    candidate["terraform_state"][role][key]
                    != current.manifest["terraform_state"][role][key]
                ):
                    raise LedgerConflict(
                        f"replacement cannot change Terraform state {role} {key}"
                    )
        old_proofs = current.manifest["terraform_state"]["restore_proofs"]
        new_proofs = candidate["terraform_state"]["restore_proofs"]
        if new_proofs[:len(old_proofs)] != old_proofs:
            raise LedgerConflict("Terraform state restore proof history is append-only")
        old_operations = current.manifest["operations"]
        new_operations = candidate["operations"]
        if new_operations[:len(old_operations)] != old_operations:
            raise LedgerConflict("provider operation history is append-only")
        old_audits = current.manifest.get("audit_receipts", [])
        new_audits = candidate.get("audit_receipts", [])
        if new_audits[:len(old_audits)] != old_audits:
            raise LedgerConflict("independent audit receipt history is append-only")
        if len(new_audits) - len(old_audits) > 1:
            raise LedgerConflict("one audit receipt may be recorded per ledger transition")
        audit_delta = len(new_audits) - len(old_audits)
        if audit_delta == 1:
            appended = new_audits[-1]
            if event_type != "audit_receipt_recorded":
                raise LedgerConflict("audit receipts may only be added by the audit operation")
            if lease_owner != f"auditor:{appended['auditor_id']}":
                raise LedgerConflict("audit receipt requires its named auditor lease")
            if appended["manifest_generation"] != current.generation:
                raise LedgerConflict("audit receipt must bind the current manifest generation")
        elif event_type == "audit_receipt_recorded":
            raise LedgerConflict("audit receipt event requires exactly one appended receipt")
        if len(new_audits) == len(old_audits) and candidate.get(
            "independent_audit"
        ) != current.manifest.get("independent_audit"):
            raise LedgerConflict("independent audit summary can change only with a receipt")
        if current.manifest["state"] == "closed" and candidate != current.manifest:
            raise LedgerConflict("closed run manifests are immutable")
        if current.generation != expected_generation:
            if (
                candidate == current.manifest
                and current.metadata["lease"]["owner"] == lease_owner
                and current.metadata["events"][-1]["type"] == event_type
            ):
                return current
            raise GenerationConflict("manifest generation is stale")
        lease = current.metadata["lease"]
        if lease["owner"] != lease_owner or lease["expires_at"] is None:
            raise LedgerConflict("manifest replacement requires the current lease owner")
        if _parse_time(lease["expires_at"], "lease.expires_at") <= at:
            raise LedgerConflict("expired lease cannot mutate the manifest")
        if candidate["state"] != current.manifest["state"]:
            try:
                next_state(current.manifest["state"], candidate["state"])
            except ContractError as exc:
                raise LedgerConflict(str(exc)) from exc
        metadata = self._append_event(current.metadata, event_type, at)
        envelope = {"schema": LEDGER_SCHEMA, "manifest": candidate, "metadata": metadata}
        try:
            return self._replace_envelope(run_id, current, envelope)
        except GenerationConflict:
            latest = self.read(run_id)
            if (
                latest.manifest == candidate
                and latest.metadata["lease"]["owner"] == lease_owner
                and latest.metadata["events"][-1]["type"] == event_type
            ):
                return latest
            raise

    def record_operation_intent(
        self,
        run_id: str,
        *,
        expected_generation: int,
        lease_owner: str,
        attempt: int,
        action: str,
        resource_type: str,
        resource_id: str,
        request_sha256: str,
        now: datetime | None = None,
    ) -> LedgerSnapshot:
        """Persist an exact provider request before its caller makes the request.

        A repeated call with the same attempt returns the existing event. That
        event is not permission to repeat the provider call: a pending attempt
        must first be reconciled by exact resource ID after any interruption.
        """
        self._validate_generation(expected_generation)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ContractError("provider operation attempt must be a positive integer")
        if attempt > MAX_OPERATION_ATTEMPTS:
            raise ContractError(
                f"provider operation attempts cannot exceed {MAX_OPERATION_ATTEMPTS}"
            )
        if (
            not isinstance(request_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", request_sha256) is None
        ):
            raise ContractError("provider request fingerprint must be a SHA-256 hex digest")
        at = now or self._clock()
        current = self.read(run_id)
        self._require_active_lease(current, lease_owner, at)
        prior_attempts = [
            event
            for event in current.manifest["operations"]
            if event["kind"] == "intent"
            and event["attempt"] == attempt
            and event["action"] == action
            and event["resource_type"] == resource_type
            and event["resource_id"] == resource_id
        ]
        if prior_attempts:
            prior = prior_attempts[-1]
            if prior["request_sha256"] != request_sha256:
                raise LedgerConflict("provider attempt already has a different request")
            # The current lifecycle phase may have advanced while this request
            # was unresolved. Return the original intent for reconciliation;
            # callers must not interpret this replay as permission to call GCP.
            return current

        if action == "create" and _parse_time(
            current.manifest["deadline_at"], "deadline_at"
        ) <= at:
            raise ContractError("new provider create cannot start at or after the run deadline")
        phase = current.manifest["state"]
        op_id = operation_id(
            run_id, attempt, action, resource_type, resource_id, request_sha256, phase
        )
        intent = {
            "kind": "intent",
            "operation_id": op_id,
            "attempt": attempt,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "request_sha256": request_sha256,
            "phase": phase,
            "at": _format_time(at),
        }
        for event in current.manifest["operations"]:
            if event["kind"] == "intent" and event["operation_id"] == op_id:
                if {
                    key: value for key, value in event.items()
                    if key not in {"sequence", "at"}
                } != {key: value for key, value in intent.items() if key != "at"}:
                    raise LedgerConflict("operation ID already has a different intent")
                return current

        if current.generation != expected_generation:
            raise GenerationConflict("manifest generation is stale")

        candidate = json.loads(json.dumps(current.manifest))
        intent["sequence"] = len(candidate["operations"]) + 1
        candidate["operations"].append(intent)
        return self.replace(
            run_id,
            expected_generation=expected_generation,
            lease_owner=lease_owner,
            manifest=candidate,
            event_type="operation_intent_recorded",
            now=at,
        )

    def record_operation_progress(
        self,
        run_id: str,
        *,
        expected_generation: int,
        lease_owner: str,
        operation_id_value: str,
        provider_operation_name: str,
        response_sha256: str,
        now: datetime | None = None,
    ) -> LedgerSnapshot:
        """Persist a provider's accepted long-running operation receipt."""
        self._validate_generation(expected_generation)
        if not isinstance(response_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", response_sha256
        ) is None:
            raise ContractError("provider progress fingerprint must be a SHA-256 hex digest")
        at = now or self._clock()
        current = self.read(run_id)
        self._require_active_lease(current, lease_owner, at)
        intent_ids = {
            event["operation_id"]
            for event in current.manifest["operations"]
            if event["kind"] == "intent"
        }
        if operation_id_value not in intent_ids:
            raise LedgerConflict("provider progress has no matching durable intent")
        matching_progress = [
            event
            for event in current.manifest["operations"]
            if event["kind"] == "progress"
            and event["operation_id"] == operation_id_value
        ]
        if matching_progress:
            if any(
                event["provider_operation_name"] != provider_operation_name
                for event in matching_progress
            ):
                raise LedgerConflict("provider operation name cannot change during reconciliation")
            if any(
                event["response_sha256"] == response_sha256
                for event in matching_progress
            ):
                return current
        if any(
            event["kind"] == "result" and event["operation_id"] == operation_id_value
            for event in current.manifest["operations"]
        ):
            raise LedgerConflict("completed provider operation cannot accept more progress")

        if current.generation != expected_generation:
            raise GenerationConflict("manifest generation is stale")

        progress = {
            "kind": "progress",
            "operation_id": operation_id_value,
            "provider_operation_name": provider_operation_name,
            "response_sha256": response_sha256,
            "at": _format_time(at),
            "sequence": len(current.manifest["operations"]) + 1,
        }
        candidate = json.loads(json.dumps(current.manifest))
        candidate["operations"].append(progress)
        return self.replace(
            run_id,
            expected_generation=expected_generation,
            lease_owner=lease_owner,
            manifest=candidate,
            event_type="operation_progress_recorded",
            now=at,
        )

    def record_operation_result(
        self,
        run_id: str,
        *,
        expected_generation: int,
        lease_owner: str,
        operation_id_value: str,
        outcome: str,
        response_sha256: str,
        provider_operation_name: str | None = None,
        now: datetime | None = None,
    ) -> LedgerSnapshot:
        """Persist a minimized provider outcome under the current lease."""
        self._validate_generation(expected_generation)
        at = now or self._clock()
        current = self.read(run_id)
        self._require_active_lease(current, lease_owner, at)
        result = {
            "kind": "result",
            "operation_id": operation_id_value,
            "outcome": outcome,
            "provider_operation_name": provider_operation_name,
            "response_sha256": response_sha256,
            "at": _format_time(at),
        }
        existing_intents = {
            event["operation_id"]
            for event in current.manifest["operations"]
            if event["kind"] == "intent"
        }
        if operation_id_value not in existing_intents:
            raise LedgerConflict("provider result has no matching durable intent")
        for event in current.manifest["operations"]:
            if event["kind"] == "result" and event["operation_id"] == operation_id_value:
                same_result = all(
                    event.get(key) == value
                    for key, value in result.items()
                    if key not in {"kind", "at"}
                )
                if same_result:
                    return current
                raise LedgerConflict("provider operation already has a different result")

        if current.generation != expected_generation:
            raise GenerationConflict("manifest generation is stale")

        candidate = json.loads(json.dumps(current.manifest))
        intent = next(
            event
            for event in candidate["operations"]
            if event["kind"] == "intent"
            and event["operation_id"] == operation_id_value
        )
        if intent["resource_type"] in DELETE_ORDER:
            resource = next(
                (
                    item for item in candidate["resources"]
                    if item["type"] == intent["resource_type"]
                    and item["id"] == intent["resource_id"]
                ),
                None,
            )
            if resource is None:
                raise LedgerConflict("provider operation target is not declared in run inventory")
            if intent["action"] == "create" and outcome in {
                "succeeded", "reconciled_present"
            }:
                resource["status"] = "present"
            elif intent["action"] == "delete" and outcome in {
                "succeeded", "reconciled_absent"
            }:
                resource["status"] = "deleted"
        result["sequence"] = len(candidate["operations"]) + 1
        candidate["operations"].append(result)
        return self.replace(
            run_id,
            expected_generation=expected_generation,
            lease_owner=lease_owner,
            manifest=candidate,
            event_type="operation_result_recorded",
            now=at,
        )

    def record_independent_audit(
        self,
        run_id: str,
        *,
        expected_generation: int,
        lease_owner: str,
        auditor_id: str,
        phase: str,
        result: str,
        complete: bool,
        inventory_sha256: str,
        evidence_sha256: str,
        checks: dict,
        observed_at: datetime | None = None,
        now: datetime | None = None,
    ) -> LedgerSnapshot:
        """Append an independent workload or closure audit receipt.

        The caller supplies digests for the external inventory and evidence
        artifact; raw provider listings remain outside the run manifest. The
        auditor must hold a separately named lease and provide Google's stable
        user subject. GCP collectors verify the expected email through UserInfo,
        but this local ledger contract does not authenticate the cloud API
        caller. A retry after a lost write response must reuse the same
        observation time, fingerprints and checks so it reconciles to the
        original receipt.
        """
        self._validate_generation(expected_generation)
        if not isinstance(auditor_id, str) or re.fullmatch(
            r"google-user-[A-Za-z0-9._~-]{1,255}",
            auditor_id,
        ) is None:
            raise ContractError("audit receipt auditor_id must be a Google user subject")
        if lease_owner != f"auditor:{auditor_id}":
            raise LedgerConflict("independent audit requires the named auditor lease")
        at = now or self._clock()
        observed_time = observed_at or at
        observed_at_text = _format_time(observed_time)
        if observed_time > at:
            raise ContractError("audit observation cannot be later than its ledger receipt")
        current = self.read(run_id)
        self._require_active_lease(current, lease_owner, at)
        if phase == "workload":
            expected_checks = {"resource_inventory_complete", "resources_absent"}
        elif phase == "closure":
            expected_checks = {
                "resource_inventory_complete", "resources_absent", "project_absent",
                "project_services_reconciled", "billing_unlinked",
                "budget_reconciled", "recovery_controls_absent",
            }
        else:
            raise ContractError("audit receipt phase is unsupported")
        if not isinstance(checks, dict) or set(checks) != expected_checks or any(
            not isinstance(value, bool) for value in checks.values()
        ):
            raise ContractError("audit receipt checks do not match its phase")
        if not isinstance(complete, bool):
            raise ContractError("audit receipt complete must be boolean")
        if not isinstance(inventory_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", inventory_sha256
        ) is None:
            raise ContractError("audit inventory fingerprint must be a SHA-256 hex digest")
        if not isinstance(evidence_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", evidence_sha256
        ) is None:
            raise ContractError("audit evidence fingerprint must be a SHA-256 hex digest")
        if not isinstance(result, str) or result not in {
            "passed", "failed", "incomplete"
        }:
            raise ContractError("audit receipt result is unsupported")
        all_checks_passed = all(checks.values())
        if result == "passed" and (not complete or not all_checks_passed):
            raise ContractError("passed audit requires complete coverage and passing checks")
        if result == "failed" and all_checks_passed:
            raise ContractError("failed audit requires at least one failed check")
        if result == "incomplete" and complete:
            raise ContractError("incomplete audit cannot claim complete coverage")
        stable_fields = {
            "run_id": run_id,
            "project_id": current.manifest["scope"]["project_id"],
            "recovery_project_id": current.manifest["scope"]["recovery_project_id"],
            "phase": phase,
            "result": result,
            "complete": complete,
            "auditor_id": auditor_id,
            "observed_at": observed_at_text,
            "inventory_sha256": inventory_sha256,
            "evidence_sha256": evidence_sha256,
            "checks": checks,
        }
        same_observation = [
            receipt
            for receipt in current.manifest.get("audit_receipts", [])
            if receipt["phase"] == phase
            and receipt["auditor_id"] == auditor_id
            and receipt["observed_at"] == observed_at_text
        ]
        if same_observation:
            existing = same_observation[-1]
            if all(existing.get(key) == value for key, value in stable_fields.items()):
                return current
            raise LedgerConflict("audit observation key already has different evidence")
        if current.generation != expected_generation:
            raise GenerationConflict("manifest generation is stale")
        prior_audits = current.manifest.get("audit_receipts", [])
        if prior_audits and observed_at_text < prior_audits[-1]["observed_at"]:
            raise ContractError("audit observation timestamps must be monotonic")
        if phase == "workload" and current.manifest["state"] != "auditing":
            raise LedgerConflict("workload audit is only accepted in auditing state")
        if phase == "closure":
            if current.manifest["state"] != "controls_reclaiming":
                raise LedgerConflict("closure audit requires controls_reclaiming state")
            if any(
                item["status"] != "deleted"
                for item in current.manifest["resources"] + current.manifest["controls"]
            ):
                raise LedgerConflict(
                    "closure audit requires all workload and control resources deleted"
                )
            prior = current.manifest.get("independent_audit")
            if (
                not isinstance(prior, dict) or prior.get("phase") != "workload"
                or prior.get("result") != "passed"
            ):
                raise LedgerConflict("closure audit requires a passed workload audit")
        receipt = {
            "sequence": len(current.manifest.get("audit_receipts", [])) + 1,
            "run_id": run_id,
            "project_id": current.manifest["scope"]["project_id"],
            "recovery_project_id": current.manifest["scope"]["recovery_project_id"],
            "phase": phase,
            "result": result,
            "complete": complete,
            "auditor_id": auditor_id,
            "observed_at": observed_at_text,
            "inventory_sha256": inventory_sha256,
            "evidence_sha256": evidence_sha256,
            "manifest_generation": current.generation,
            "checks": json.loads(json.dumps(checks)),
        }
        receipt["receipt_id"] = hashlib.sha256(_canonical_json(receipt)).hexdigest()
        candidate = json.loads(json.dumps(current.manifest))
        candidate.setdefault("audit_receipts", []).append(receipt)
        candidate["independent_audit"] = _audit_summary(receipt)
        return self.replace(
            run_id,
            expected_generation=expected_generation,
            lease_owner=lease_owner,
            manifest=candidate,
            event_type="audit_receipt_recorded",
            now=at,
        )

    @staticmethod
    def _require_active_lease(
        current: LedgerSnapshot,
        lease_owner: str,
        at: datetime,
    ) -> None:
        lease = current.metadata["lease"]
        if lease["owner"] != lease_owner or lease["expires_at"] is None:
            raise LedgerConflict("provider operation record requires the current lease owner")
        if _parse_time(lease["expires_at"], "lease.expires_at") <= at:
            raise LedgerConflict("expired lease cannot record a provider operation")

    @staticmethod
    def _validate_ttl(ttl: timedelta) -> None:
        if not isinstance(ttl, timedelta) or ttl < MIN_LEASE or ttl > MAX_LEASE:
            raise LedgerError("lease ttl must be between 15 seconds and 5 minutes")

    @staticmethod
    def _validate_generation(generation: int) -> None:
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise LedgerError("expected_generation must be a positive integer")


class GcsObjectStore:
    """Google Cloud Storage object adapter using generation-match preconditions.

    `google-cloud-storage` is imported lazily so local contracts and fake-store
    tests do not need credentials or a cloud SDK installation.
    """

    def __init__(self, bucket_name: str):
        if not isinstance(bucket_name, str) or not bucket_name:
            raise ValueError("bucket_name must be non-empty")
        try:
            from google.api_core.exceptions import NotFound, PreconditionFailed
            from google.cloud import storage
        except ImportError as exc:
            raise LedgerError("google-cloud-storage is required for GCS access") from exc
        self._bucket = storage.Client().bucket(bucket_name)
        self._not_found = NotFound
        self._precondition_failed = PreconditionFailed

    def read(self, name: str) -> tuple[bytes, int]:
        blob = self._bucket.blob(name)
        try:
            blob.reload()
            if blob.generation is None:
                raise LedgerError("GCS did not return an object generation")
            generation = int(blob.generation)
            content = blob.download_as_bytes(if_generation_match=generation)
        except self._not_found as exc:
            raise LedgerNotFound(name) from exc
        except self._precondition_failed as exc:
            raise GenerationConflict(name) from exc
        return content, generation

    def create(self, name: str, content: bytes) -> int:
        blob = self._bucket.blob(name)
        try:
            blob.upload_from_string(
                content,
                content_type="application/json",
                if_generation_match=0,
            )
        except self._precondition_failed as exc:
            raise GenerationConflict(name) from exc
        if blob.generation is None:
            raise LedgerError("GCS did not return the created object generation")
        return int(blob.generation)

    def replace(self, name: str, content: bytes, *, generation: int) -> int:
        blob = self._bucket.blob(name)
        try:
            blob.upload_from_string(
                content,
                content_type="application/json",
                if_generation_match=generation,
            )
        except self._precondition_failed as exc:
            raise GenerationConflict(name) from exc
        if blob.generation is None:
            raise LedgerError("GCS did not return the replaced object generation")
        return int(blob.generation)
