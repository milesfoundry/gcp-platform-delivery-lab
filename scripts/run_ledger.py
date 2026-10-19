"""Generation-conditional storage for exact-path GCP run manifests.

The module never lists or deletes manifests and does not mutate cloud resources.
A caller must know the run ID and expected generation for every replacement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Protocol

from run_contract import (
    ContractError,
    RUN_ID,
    next_state,
    validate_intent,
    validate_manifest,
)


class LedgerError(RuntimeError):
    """Base class for durable ledger failures."""


class LedgerNotFound(LedgerError):
    """The exact run manifest does not exist."""


class GenerationConflict(LedgerError):
    """The object changed since the caller read it."""


class LedgerConflict(LedgerError):
    """An immutable run identity or transition was changed."""


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
            raise LedgerError(f"manifest contains a duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_manifest(content: bytes) -> dict:
    try:
        value = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError("stored manifest is not valid UTF-8 JSON") from exc
    try:
        return validate_manifest(value)
    except ContractError as exc:
        raise LedgerError(f"stored manifest violates the run contract: {exc}") from exc


class RunLedger:
    """Create, read, and compare-and-swap one run manifest at a time."""

    def __init__(self, store: ObjectStore, *, clock=None):
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create(self, intent: object) -> LedgerSnapshot:
        """Create a run manifest once; safely reconcile an identical retry."""
        try:
            manifest = validate_intent(intent, now=self._clock())
        except ContractError as contract_error:
            raw_run_id = intent.get("run_id") if isinstance(intent, dict) else None
            object_name(raw_run_id)
            try:
                body = _canonical_json(intent)
                existing = self.read(raw_run_id)
            except LedgerNotFound:
                raise contract_error
            if _canonical_json(existing.manifest) != body:
                raise LedgerConflict("run ID already has a different manifest")
            return existing
        name = object_name(manifest["run_id"])
        body = _canonical_json(manifest)
        try:
            generation = self._store.create(name, body)
            return LedgerSnapshot(manifest["run_id"], manifest, generation)
        except GenerationConflict:
            # This handles a retry after the provider committed but its response
            # was lost. A different payload under the same run ID is never reused.
            existing = self.read(manifest["run_id"])
            if _canonical_json(existing.manifest) != body:
                raise LedgerConflict("run ID already has a different manifest")
            return existing

    def read(self, run_id: str) -> LedgerSnapshot:
        name = object_name(run_id)
        try:
            content, generation = self._store.read(name)
        except LedgerNotFound:
            raise
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise LedgerError("object store returned an invalid generation")
        manifest = _decode_manifest(content)
        if manifest["run_id"] != run_id:
            raise LedgerError("manifest run_id does not match its exact object path")
        return LedgerSnapshot(run_id, manifest, generation)

    def replace(
        self,
        run_id: str,
        *,
        expected_generation: int,
        manifest: object,
    ) -> LedgerSnapshot:
        """Replace one manifest only if the stored generation is unchanged."""
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 1
        ):
            raise LedgerError("expected_generation must be a positive integer")
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
        body = _canonical_json(candidate)
        if current.generation != expected_generation:
            if _canonical_json(current.manifest) == body:
                return current
            raise GenerationConflict("manifest generation is stale")
        if candidate["state"] != current.manifest["state"]:
            try:
                next_state(current.manifest["state"], candidate["state"])
            except ContractError as exc:
                raise LedgerConflict(str(exc)) from exc
        try:
            generation = self._store.replace(
                object_name(run_id), body, generation=expected_generation
            )
        except GenerationConflict:
            # A retry after a committed write can confirm its exact payload;
            # any competing state remains a hard generation conflict.
            latest = self.read(run_id)
            if _canonical_json(latest.manifest) == body:
                return latest
            raise
        return LedgerSnapshot(run_id, candidate, generation)


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
