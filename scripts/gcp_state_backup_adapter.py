"""Generation-pinned private GCS backup and recovery-copy adapter.

The adapter never discovers credentials and never overwrites a Terraform state
object. Callers must supply an explicitly authenticated, principal-verified
token source and an HTTPS transport. The recovery copy is retained under a
run-scoped, separate object name for review; it can contain sensitive state.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Protocol
from urllib.parse import quote, urlencode

from run_contract import ContractError, validate_intent


class StateBackupError(RuntimeError):
    """A state generation or private-storage invariant could not be proven."""


class TokenSource(Protocol):
    def access_token(self) -> str: ...


class Response(Protocol):
    status_code: int

    def json(self) -> object: ...


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict,
        body: bytes | None,
        timeout: int,
        allow_redirects: bool,
    ) -> Response: ...


@dataclass(frozen=True)
class StateCopy:
    source_generation: str
    destination_generation: str
    crc32c: str
    size: str
    object_name: str


class GCPStateBackupAdapter:
    """Copy exact Terraform state generations between private GCS buckets."""

    def __init__(
        self,
        transport: Transport,
        token_source: TokenSource,
        manifest: dict,
        *,
        timeout: int = 60,
        max_rewrite_calls: int = 100,
    ):
        try:
            self._manifest = validate_intent(manifest, new_run=False)
        except ContractError as exc:
            raise StateBackupError("run manifest is invalid") from None
        if transport is None or token_source is None:
            raise ValueError("explicit Google transport and token source are required")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 30 <= timeout <= 120:
            raise ValueError("timeout must be between 30 and 120 seconds")
        if (
            isinstance(max_rewrite_calls, bool)
            or not isinstance(max_rewrite_calls, int)
            or not 1 <= max_rewrite_calls <= 100
        ):
            raise ValueError("max_rewrite_calls must be between 1 and 100")
        state = self._manifest["terraform_state"]
        if state["backend"]["project_id"] == state["backup"]["project_id"]:
            raise StateBackupError("state and backup must use separate projects")
        self._transport = transport
        self._token_source = token_source
        self._timeout = timeout
        self._max_rewrite_calls = max_rewrite_calls
        self._run_id = self._manifest["run_id"]
        self._backend = state["backend"]
        self._backup = state["backup"]
        self._backup_generation = self._backup.get("generation")

    def discover_latest_two_generations(self) -> dict[str, dict]:
        """Return the current and previous exact live Terraform state versions."""
        self._verify_private_bucket(self._backend)
        versions = []
        page_token = None
        seen_tokens = set()
        for _ in range(100):
            params = {
                "versions": "true",
                "prefix": self._backend["object"],
                "fields": "items(name,generation,crc32c,size),nextPageToken",
                "maxResults": "1000",
            }
            if page_token:
                params["pageToken"] = page_token
            response = self._request(
                "GET",
                "https://storage.googleapis.com/storage/v1/b/"
                + quote(self._backend["bucket"], safe="") + "/o",
                params=params,
            )
            items = response.get("items", [])
            self._require(isinstance(items, list), "state object version list is malformed")
            versions.extend(item for item in items if isinstance(item, dict)
                            and item.get("name") == self._backend["object"])
            token = response.get("nextPageToken")
            if not token:
                break
            self._require(
                isinstance(token, str) and token not in seen_tokens,
                "state object version pagination token repeated",
            )
            seen_tokens.add(token)
            page_token = token
        else:
            raise StateBackupError("state object version inventory exceeded the page limit")

        unique = {}
        for item in versions:
            generation = item.get("generation")
            if not isinstance(generation, (str, int)) or not str(generation).isdigit():
                raise StateBackupError("state object version has an invalid generation")
            unique[str(generation)] = item
        ordered = sorted(unique.values(), key=lambda item: int(item["generation"]), reverse=True)
        self._require(
            len(ordered) >= 2,
            "Terraform state bucket has no previous retained generation",
        )
        latest, previous = ordered[:2]
        current_generation = str(latest["generation"])
        declared_generation = self._backend.get("generation")
        self._require(
            declared_generation is None or declared_generation == current_generation,
            "manifest backend generation does not match the live current state object",
        )
        for item in (latest, previous):
            self._require(
                isinstance(item.get("crc32c"), str)
                and isinstance(item.get("size"), (str, int)),
                "state object version is missing checksum or size metadata",
            )
        return {"current": latest, "previous": previous}

    def backup_and_restore_latest_two(self) -> dict[str, dict]:
        """Back up and restore current/previous state versions to new objects."""
        self._verify_private_bucket(self._backend)
        self._verify_private_bucket(self._backup)
        snapshots = self.discover_latest_two_generations()
        # Preserve the previous copy as a noncurrent backup version, then leave
        # the current state as the backup bucket's live version.
        backup_copies = {}
        for label in ("previous", "current"):
            item = snapshots[label]
            backup_copies[label] = self.backup(str(item["generation"]))
            self._require(
                backup_copies[label].crc32c == item["crc32c"]
                and backup_copies[label].size == str(item["size"]),
                f"{label} state backup differs from its source generation",
            )

        proof_records = {}
        for label in ("current", "previous"):
            state_copy = backup_copies[label]
            restore_object = (
                f"runs/{self._run_id}/restore-proofs/"
                f"{label}-{state_copy.source_generation}.tfstate"
            )
            restored = self.restore_to_new_object(
                source_generation=state_copy.source_generation,
                backup_generation=state_copy.destination_generation,
                expected_crc32c=state_copy.crc32c,
                object_name=restore_object,
            )
            proof_records[label] = {
                "snapshot": label,
                "state_generation": state_copy.source_generation,
                "backup_generation": state_copy.destination_generation,
                "restored_generation": restored.destination_generation,
                "state_crc32c": state_copy.crc32c,
                "backup_crc32c": state_copy.crc32c,
                "restored_crc32c": restored.crc32c,
                "restored_object": restored.object_name,
            }
        return proof_records

    def backup(self, source_generation: str) -> StateCopy:
        """Back up one exact backend generation using destination CAS."""
        self._require_generation(source_generation)
        self._verify_private_bucket(self._backend)
        self._verify_private_bucket(self._backup)
        source = self._get_object(
            self._backend["bucket"], self._backend["object"], source_generation
        )
        self._verify_object(source, self._backend, source_generation)
        expected_destination = self._backup_generation
        query = {
            "sourceGeneration": source_generation,
            "ifSourceGenerationMatch": source_generation,
            "ifGenerationMatch": expected_destination or "0",
        }
        provenance = {
            "portfolioRunId": self._run_id,
            "portfolioCopyPurpose": "state-backup",
            "portfolioSourceGeneration": source_generation,
        }
        try:
            result = self._rewrite(
                self._backend["bucket"], self._backend["object"],
                self._backup["bucket"], self._backup["object"], query,
                object_metadata=provenance,
            )
            copied = result["resource"]
        except StateBackupError:
            copied = self._reconcile_copy(
                self._backup, self._backup["object"], provenance,
                expected_crc32c=source["crc32c"], expected_size=str(source["size"]),
            )
        self._verify_object(
            copied, self._backup, str(copied.get("generation", "")),
            object_name=self._backup["object"],
        )
        if (
            copied.get("crc32c") != source.get("crc32c")
            or str(copied.get("size")) != str(source.get("size"))
        ):
            raise StateBackupError("state backup checksum or size did not match its source")
        self._backup_generation = str(copied["generation"])
        return StateCopy(
            source_generation=source_generation,
            destination_generation=str(copied["generation"]),
            crc32c=source["crc32c"],
            size=str(source["size"]),
            object_name=self._backup["object"],
        )

    def restore_to_new_object(
        self,
        *,
        source_generation: str,
        backup_generation: str,
        expected_crc32c: str,
        object_name: str,
    ) -> StateCopy:
        """Restore a backup generation to a new exact run-scoped state object.

        The destination is never the active Terraform backend object and is
        created with ifGenerationMatch=0. It is retained for private operator
        review because deletion may create a retained noncurrent generation.
        """
        self._require_generation(source_generation)
        self._require_generation(backup_generation)
        prefix = f"runs/{self._run_id}/restore-proofs/"
        allowed_target = re.fullmatch(
            re.escape(prefix) + r"[a-z0-9][a-z0-9.-]{0,100}\.tfstate",
            object_name if isinstance(object_name, str) else "",
        )
        if not allowed_target:
            raise StateBackupError("restore target must be a distinct run-scoped .tfstate object")
        if not isinstance(expected_crc32c, str) or not expected_crc32c:
            raise StateBackupError("an expected backup CRC32C is required")
        self._verify_private_bucket(self._backup)
        self._verify_private_bucket(self._backend)
        original = self._get_object(
            self._backend["bucket"], self._backend["object"], source_generation
        )
        self._verify_object(original, self._backend, source_generation)
        source = self._get_object(
            self._backup["bucket"], self._backup["object"], backup_generation
        )
        self._verify_object(source, self._backup, backup_generation)
        if (
            source.get("crc32c") != expected_crc32c
            or original.get("crc32c") != expected_crc32c
            or str(source.get("size")) != str(original.get("size"))
        ):
            raise StateBackupError("state and backup generations do not match the restore proof")
        provenance = {
            "portfolioRunId": self._run_id,
            "portfolioCopyPurpose": "state-restore-proof",
            "portfolioSourceGeneration": source_generation,
            "portfolioBackupGeneration": backup_generation,
        }
        try:
            result = self._rewrite(
                self._backup["bucket"], self._backup["object"],
                self._backend["bucket"], object_name,
                {
                    "sourceGeneration": backup_generation,
                    "ifSourceGenerationMatch": backup_generation,
                    "ifGenerationMatch": "0",
                },
                object_metadata=provenance,
            )
            restored = result["resource"]
        except StateBackupError:
            restored = self._reconcile_copy(
                self._backend, object_name, provenance,
                expected_crc32c=source["crc32c"], expected_size=str(source["size"]),
            )
        self._verify_object(
            restored, self._backend, str(restored.get("generation", "")),
            object_name=object_name,
        )
        if (
            restored.get("crc32c") != source.get("crc32c")
            or str(restored.get("size")) != str(source.get("size"))
        ):
            raise StateBackupError("restored state checksum or size did not match its backup")
        return StateCopy(
            source_generation=source_generation,
            destination_generation=str(restored["generation"]),
            crc32c=source["crc32c"],
            size=str(source["size"]),
            object_name=object_name,
        )

    def _verify_private_bucket(self, reference: dict) -> None:
        bucket = self._request(
            "GET",
            "https://storage.googleapis.com/storage/v1/b/"
            + quote(reference["bucket"], safe=""),
            params={
                "fields": "name,projectNumber,iamConfiguration,versioning,retentionPolicy"
            },
        )
        expected_project = self._request(
            "GET",
            "https://cloudresourcemanager.googleapis.com/v3/projects/"
            + quote(reference["project_id"], safe=""),
            params={"fields": "name,projectId,state"},
        )
        iam = bucket.get("iamConfiguration")
        uniform = iam.get("uniformBucketLevelAccess") if isinstance(iam, dict) else None
        retention = bucket.get("retentionPolicy")
        self._require(
            bucket.get("name") == reference["bucket"]
            and expected_project.get("name") == f"projects/{bucket.get('projectNumber')}"
            and expected_project.get("projectId") == reference["project_id"]
            and expected_project.get("state") == "ACTIVE"
            and isinstance(uniform, dict) and uniform.get("enabled") is True
            and iam.get("publicAccessPrevention") == "enforced"
            and isinstance(bucket.get("versioning"), dict)
            and bucket["versioning"].get("enabled") is True
            and isinstance(retention, dict)
            and str(retention.get("retentionPeriod", "0")).isdigit()
            and int(retention.get("retentionPeriod", "0")) > 0,
            "state bucket ownership, privacy, versioning, or retention policy is not qualified",
        )

    def _get_object(self, bucket: str, object_name: str, generation: str) -> dict:
        return self._request(
            "GET",
            self._object_url(bucket, object_name),
            params={"generation": generation, "fields": "bucket,name,generation,crc32c,size"},
        )

    def _rewrite(
        self,
        source_bucket: str,
        source_object: str,
        destination_bucket: str,
        destination_object: str,
        initial_params: dict,
        *,
        object_metadata: dict,
    ) -> dict:
        url = (
            self._object_url(source_bucket, source_object)
            + "/rewriteTo/b/" + quote(destination_bucket, safe="")
            + "/o/" + quote(destination_object, safe="")
        )
        params = dict(initial_params)
        token = None
        for _ in range(self._max_rewrite_calls):
            if token:
                params = {"rewriteToken": token}
            body = (
                None if token else json.dumps(
                    {"metadata": object_metadata}, separators=(",", ":")
                ).encode()
            )
            response = self._request("POST", url, params=params, body=body)
            done = response.get("done")
            if done is True:
                resource = response.get("resource")
                self._require(
                    isinstance(resource, dict), "completed rewrite has no object metadata"
                )
                return response
            self._require(done is False, "rewrite response has invalid completion state")
            token = response.get("rewriteToken")
            self._require(
                isinstance(token, str) and bool(token),
                "incomplete rewrite has no continuation token",
            )
        raise StateBackupError("rewrite exceeded the bounded continuation-call limit")

    def _reconcile_copy(
        self,
        reference: dict,
        object_name: str,
        expected_metadata: dict,
        *,
        expected_crc32c: str,
        expected_size: str,
    ) -> dict:
        """Recognize a completed exact copy after an ambiguous response."""
        latest = self._request(
            "GET", self._object_url(reference["bucket"], object_name),
            params={"fields": "bucket,name,generation,crc32c,size,metadata"},
        )
        self._verify_object(
            latest, reference, str(latest.get("generation", "")),
            object_name=object_name,
        )
        self._require(
            latest.get("metadata") == expected_metadata
            and latest.get("crc32c") == expected_crc32c
            and str(latest.get("size")) == expected_size,
            "ambiguous state copy could not be reconciled to the exact run and source generation",
        )
        return latest

    def _request(
        self, method: str, url: str, *, params: dict | None = None,
        body: bytes | None = None,
    ) -> dict:
        token = self._token_source.access_token()
        if not isinstance(token, str) or not token:
            raise StateBackupError("explicit Google token source returned no token")
        if params:
            url += "?" + urlencode(params)
        try:
            response = self._transport.request(
                method, url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                body=body, timeout=self._timeout, allow_redirects=False,
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise StateBackupError(
                    f"Google Cloud Storage request failed with HTTP {response.status_code}"
                )
            payload = response.json()
        except StateBackupError:
            raise
        except Exception:
            raise StateBackupError("state storage request failed") from None
        self._require(isinstance(payload, dict), "Google API returned a malformed response")
        return payload

    def _verify_object(
        self, value: dict, reference: dict, generation: str, *, object_name: str | None = None
    ) -> None:
        self._require(
            value.get("bucket") == reference["bucket"]
            and value.get("name") == (object_name or reference["object"])
            and isinstance(value.get("generation"), (str, int))
            and str(value.get("generation")) == generation
            and isinstance(value.get("crc32c"), str)
            and isinstance(value.get("size"), (str, int)),
            "object metadata does not match the exact expected bucket and generation",
        )

    @staticmethod
    def _object_url(bucket: str, object_name: str) -> str:
        return (
            "https://storage.googleapis.com/storage/v1/b/"
            + quote(bucket, safe="") + "/o/" + quote(object_name, safe="")
        )

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise StateBackupError(message)

    @staticmethod
    def _require_generation(value: str) -> None:
        if not isinstance(value, str) or not value.isdigit() or int(value) <= 0:
            raise StateBackupError("object generation must be a positive integer string")
