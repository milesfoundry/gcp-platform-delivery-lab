"""Read-only verification of the exact per-run GCP recovery boundary."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from urllib.parse import quote, urlencode

from run_contract import ContractError, validate_intent


class RecoveryControlVerificationError(RuntimeError):
    """A required recovery control was absent, mismatched, or unreadable."""


@dataclass(frozen=True)
class _Read:
    status: int
    payload: dict | None
    response_sha256: str


class GCPRecoveryControlVerifier:
    """Verify recovery controls using exact, read-only Google API GET calls.

    The supplied transport and token source are explicit dependencies. This
    verifier performs no credential discovery and never writes to GCP.
    """

    def __init__(self, transport, token_source, manifest: dict, *, timeout: int = 30):
        try:
            self._manifest = validate_intent(manifest, new_run=False)
        except ContractError as exc:
            raise RecoveryControlVerificationError(str(exc)) from None
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
            raise ValueError("timeout must be between 1 and 60 seconds")
        if transport is None or token_source is None:
            raise ValueError("explicit Google transport and token source are required")
        self._transport = transport
        self._token_source = token_source
        self._timeout = timeout
        self._project = self._manifest["scope"]["recovery_project_id"]
        self._run_project = self._manifest["scope"]["project_id"]
        self._run_id = self._manifest["run_id"]
        self._region = self._manifest["scope"]["region"]
        self._bucket = self._manifest["scope"]["ledger_bucket_name"]
        self._reaper_email = self._manifest["cleanup"]["service_account"]
        self._scheduler_email = f"{self._run_id}-sched@{self._project}.iam.gserviceaccount.com"
        self._controls = {
            (item["type"], item["id"]): item for item in self._manifest["controls"]
        }

    def verify(self) -> list[dict]:
        """Return one response digest for each exact predeclared control."""
        expected = {
            "gcp.run.job": f"projects/{self._project}/locations/{self._region}/jobs/{self._run_id}-reaper",
            "gcp.scheduler.job": f"projects/{self._project}/locations/{self._region}/jobs/{self._run_id}-expiry",
            "gcp.iam.service_account": {
                f"projects/{self._project}/serviceAccounts/{self._reaper_email}",
                f"projects/{self._project}/serviceAccounts/{self._scheduler_email}",
            },
        }
        self._require(
            self._manifest["state"] == "control_bootstrapping"
            and all(item["status"] == "expected" for item in self._manifest["controls"]),
            "recovery controls must be verified from the initial bootstrap state",
        )
        for (resource_type, resource_id) in self._controls:
            declared = expected.get(resource_type)
            matches = (
                resource_id == declared if isinstance(declared, str)
                else resource_id in (declared or set())
            )
            if not matches:
                raise RecoveryControlVerificationError("manifest recovery control IDs do not match deterministic run names")
        if len(self._controls) != 4:
            raise RecoveryControlVerificationError("manifest must predeclare exactly four recovery controls")

        reaper_account = self._read_service_account(self._reaper_email, "reaper")
        scheduler_account = self._read_service_account(self._scheduler_email, "scheduler")
        project_number = self._read_recovery_project_number()
        ledger_metadata = self._get(
            "storage.googleapis.com", f"/storage/v1/b/{quote(self._bucket, safe='')}",
            params={"fields": "name,projectNumber,iamConfiguration"},
        )
        ledger_iam = ledger_metadata.payload.get("iamConfiguration")
        ubla = ledger_iam.get("uniformBucketLevelAccess") if isinstance(ledger_iam, dict) else None
        self._require(
            ledger_metadata.payload.get("name") == self._bucket
            and ledger_metadata.payload.get("projectNumber") == project_number
            and isinstance(ubla, dict) and ubla.get("enabled") is True
            and ledger_iam.get("publicAccessPrevention") == "enforced",
            "ledger bucket must belong to the recovery project and enforce uniform and public access prevention",
        )
        role_id = "portfolioLedgerReaper" + self._run_id.replace("-", "")
        role_name = f"projects/{self._project}/roles/{role_id}"
        role = self._get(
            "iam.googleapis.com", "/v1/" + quote(role_name, safe="/")
        )
        self._require(
            role.payload.get("name") == role_name
            and role.payload.get("title") == f"Portfolio {self._run_id} exact ledger access"
            and role.payload.get("description") == "Read and replace only this run's manifest object."
            and role.payload.get("stage") == "GA"
            and role.payload.get("deleted") is not True
            and sorted(role.payload.get("includedPermissions", [])) == sorted([
                "storage.objects.create", "storage.objects.delete", "storage.objects.get",
            ]),
            "ledger reaper custom role does not match the reviewed minimum permission set",
        )
        bucket_policy = self._get(
            "storage.googleapis.com",
            f"/storage/v1/b/{quote(self._bucket, safe='')}/iam",
            params={"optionsRequestedPolicyVersion": "3"},
        )
        ledger_member = f"serviceAccount:{self._reaper_email}"
        ledger_bindings = [
            binding for binding in bucket_policy.payload.get("bindings", [])
            if binding.get("role") == role_name
        ]
        all_bucket_bindings = bucket_policy.payload.get("bindings", [])
        self._require(isinstance(all_bucket_bindings, list), "ledger bucket IAM policy bindings are malformed")
        self._require(
            all(isinstance(binding, dict) and isinstance(binding.get("members", []), list)
                for binding in all_bucket_bindings),
            "ledger bucket IAM policy members are malformed",
        )
        ledger_condition = {
            "title": f"Exact manifest for {self._run_id}",
            "description": "Restrict ledger access to the one durable manifest object.",
            "expression": (
                f"resource.name == 'projects/_/buckets/{self._bucket}/objects/"
                f"runs/{self._run_id}/manifest.json'"
            ),
        }
        self._require(
            len(ledger_bindings) == 1
            and ledger_bindings[0].get("members") == [ledger_member]
            and ledger_bindings[0].get("condition") == ledger_condition,
            "ledger bucket policy must grant only the reaper identity on this exact manifest object",
        )
        self._require(
            not any(
                member in {"allUsers", "allAuthenticatedUsers"}
                or (member == ledger_member and binding not in ledger_bindings)
                for binding in all_bucket_bindings
                for member in binding.get("members", [])
            ),
            "ledger bucket must not grant broader direct access to the reaper or the public",
        )

        job_name = expected["gcp.run.job"]
        job = self._get("run.googleapis.com", "/v2/" + quote(job_name, safe="/"))
        job_policy = self._get(
            "run.googleapis.com", "/v2/" + quote(job_name, safe="/") + ":getIamPolicy"
        )
        run_invokers = [
            binding for binding in job_policy.payload.get("bindings", [])
            if binding.get("role") == "roles/run.invoker"
        ]
        self._require(
            len(run_invokers) == 1
            and run_invokers[0].get("members") == [f"serviceAccount:{self._scheduler_email}"]
            and "condition" not in run_invokers[0],
            "reaper Job invoker policy must contain only its matching Scheduler identity",
        )
        expected_env = {
            "PORTFOLIO_RUN_ID": self._run_id,
            "PORTFOLIO_PROJECT_ID": self._run_project,
            "PORTFOLIO_RECOVERY_PROJECT_ID": self._project,
            "PORTFOLIO_RUN_PARENT": self._manifest["scope"]["parent"],
            "PORTFOLIO_BILLING_ACCOUNT": self._manifest["finance"]["billing_account_id"],
            "PORTFOLIO_LEDGER_BUCKET": self._bucket,
            "PORTFOLIO_ENABLED_SERVICES": json.dumps(
                self._manifest["scope"]["enabled_api_services"], separators=(",", ":")
            ),
            "PORTFOLIO_REAPER_EMAIL": self._reaper_email,
            "PORTFOLIO_REAPER_SERVICE_ACCOUNT_ID": reaper_account.payload.get("uniqueId"),
            "PORTFOLIO_IDENTITY_AUDIENCE": self._manifest["cleanup"]["identity_audience"],
        }
        template = job.payload.get("template")
        template = template if isinstance(template, dict) else {}
        execution = template.get("template")
        execution = execution if isinstance(execution, dict) else {}
        containers = execution.get("containers", [])
        containers = containers if isinstance(containers, list) else []
        first_container = containers[0] if len(containers) == 1 and isinstance(containers[0], dict) else {}
        env_list = first_container.get("env", [])
        env_list = env_list if isinstance(env_list, list) else []
        env = {
            item.get("name"): item.get("value")
            for item in env_list if isinstance(item, dict)
        }
        self._require(
            job.payload.get("name") == job_name
            and job.payload.get("reconciling", False) is False
            and isinstance(job.payload.get("terminalCondition"), dict)
            and job.payload["terminalCondition"].get("state") == "CONDITION_SUCCEEDED"
            and template.get("taskCount") == 1
            and template.get("parallelism") == 1
            and execution.get("serviceAccount") == self._reaper_email
            and execution.get("timeout") == "90s"
            and execution.get("maxRetries", 0) == 0
            and len(containers) == 1
            and first_container.get("image") == self._manifest["cleanup"]["image_digest"]
            and len(env_list) == len(expected_env)
            and env == expected_env,
            "Cloud Run reaper Job differs from the exact run intent or pinned runtime",
        )

        scheduler_name = expected["gcp.scheduler.job"]
        scheduler = self._get(
            "cloudscheduler.googleapis.com", "/v1/" + quote(scheduler_name, safe="/")
        )
        self._require(
            scheduler.payload.get("name") == scheduler_name
            and scheduler.payload.get("state") == "ENABLED"
            and scheduler.payload.get("schedule") == "*/5 * * * *"
            and scheduler.payload.get("timeZone") == "Europe/London"
            and scheduler.payload.get("attemptDeadline") == "320s"
            and isinstance(scheduler.payload.get("retryConfig", {}), dict)
            and scheduler.payload.get("retryConfig", {}).get("retryCount", 0) == 0
            and scheduler.payload.get("httpTarget") == {
                "uri": f"https://run.googleapis.com/v2/{job_name}:run",
                "httpMethod": "POST",
                "body": "e30=",
                "oauthToken": {
                    "serviceAccountEmail": self._scheduler_email,
                    "scope": "https://www.googleapis.com/auth/cloud-platform",
                },
            },
            "Cloud Scheduler job is not the enabled five-minute trigger for this exact reaper Job",
        )

        run_project_absence = self._get(
            "cloudresourcemanager.googleapis.com",
            f"/v3/projects/{quote(self._run_project, safe='')}",
            allow_not_found=True,
        )
        self._require(
            run_project_absence.status == 404,
            "the disposable run project already exists before recovery controls were verified",
        )

        digests = {
            ("gcp.iam.service_account", f"projects/{self._project}/serviceAccounts/{self._reaper_email}"): _digest({
                "service_account": reaper_account.payload,
                "bucket": ledger_metadata.payload,
                "bucket_policy": bucket_policy.payload,
                "ledger_role": role.payload,
            }),
            ("gcp.iam.service_account", f"projects/{self._project}/serviceAccounts/{self._scheduler_email}"): scheduler_account.response_sha256,
            ("gcp.run.job", job_name): _digest({
                "job": job.payload, "job_policy": job_policy.payload,
                "run_project_absence_status": run_project_absence.status,
            }),
            ("gcp.scheduler.job", scheduler_name): scheduler.response_sha256,
        }
        return [
            {"type": resource_type, "id": resource_id,
             "response_sha256": digests[(resource_type, resource_id)]}
            for resource_type, resource_id in sorted(self._controls)
        ]

    def _read_service_account(self, email: str, purpose: str) -> _Read:
        name = f"projects/{self._project}/serviceAccounts/{email}"
        result = self._get("iam.googleapis.com", "/v1/" + quote(name, safe="/"))
        descriptions = {
            "reaper": (
                f"Portfolio run {self._run_id} retirement job",
                "Per-run retirement identity. It cannot create projects or recovery controls.",
            ),
            "scheduler": (
                f"Portfolio run {self._run_id} Scheduler invocation",
                "May invoke only the matching per-run Cloud Run Job.",
            ),
        }
        display_name, description = descriptions[purpose]
        self._require(
            result.payload.get("name") == name
            and result.payload.get("projectId") == self._project
            and result.payload.get("email") == email
            and result.payload.get("disabled", False) is False
            and result.payload.get("displayName") == display_name
            and result.payload.get("description") == description
            and isinstance(result.payload.get("uniqueId"), str)
            and result.payload["uniqueId"].isdigit(),
            f"per-run {purpose} service account is missing, disabled, or mismatched",
        )
        return result

    def _read_recovery_project_number(self) -> str:
        result = self._get(
            "cloudresourcemanager.googleapis.com",
            f"/v3/projects/{quote(self._project, safe='')}",
        )
        self._require(
            result.payload.get("projectId") == self._project
            and isinstance(result.payload.get("projectNumber"), str)
            and result.payload["projectNumber"].isdigit(),
            "recovery project metadata does not match the run intent",
        )
        return result.payload["projectNumber"]

    def _get(self, host: str, path: str, *, params: dict | None = None, allow_not_found: bool = False) -> _Read:
        allowed_hosts = {
            "iam.googleapis.com", "run.googleapis.com", "cloudscheduler.googleapis.com",
            "storage.googleapis.com", "cloudresourcemanager.googleapis.com",
        }
        if host not in allowed_hosts or not path.startswith("/") or ".." in path.split("/"):
            raise RecoveryControlVerificationError("GCP read target is outside the fixed API allowlist")
        try:
            token = self._token_source.access_token()
        except Exception:
            raise RecoveryControlVerificationError("explicit Google access token is unavailable") from None
        if not isinstance(token, str) or not token or token.strip() != token or "\n" in token or "\r" in token:
            raise RecoveryControlVerificationError("Google access token source returned an invalid token")
        query = "?" + urlencode(params) if params else ""
        try:
            response = self._transport.request(
                "GET", f"https://{host}{path}{query}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                body=None, timeout=self._timeout, allow_redirects=False,
            )
        except Exception:
            raise RecoveryControlVerificationError("GCP recovery-control read failed") from None
        if allow_not_found and response.status_code == 404:
            payload = {"status": 404}
            return _Read(404, payload, _digest(payload))
        if response.status_code != 200:
            raise RecoveryControlVerificationError(
                f"GCP recovery-control read returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except Exception:
            raise RecoveryControlVerificationError("GCP recovery-control response was malformed") from None
        if not isinstance(payload, dict):
            raise RecoveryControlVerificationError("GCP recovery-control response was not an object")
        return _Read(200, payload, _digest(payload))

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise RecoveryControlVerificationError(message)


def _digest(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
