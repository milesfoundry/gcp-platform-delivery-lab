"""Exact-project GCP create, retire, bill, and API-enable adapter.

This adapter is deliberately separate from workload cleanup. It accepts only
the run project declared at construction, one reviewed parent, one billing
account, and the exact API allowlist. Project retirement is a separately gated
teardown operation. Live use is not qualified.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import quote, urlsplit

from gcp_cleanup_adapter import TokenSource, Transport, UrllibTransport
from gcp_token_identity import (
    IdentityTransport,
    IdentityVerificationError,
    VerifiedGoogleUser,
    verify_access_token_principal,
)
from recovery_controller import ProviderReceipt


PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
RUN_ID = re.compile(r"^lab-[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?$")
BILLING_ACCOUNT = re.compile(
    r"^billingAccounts/[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$"
)
PARENT = re.compile(r"^(?:organizations|folders)/[0-9]{1,20}$")
API_NAME = re.compile(r"^[a-z][a-z0-9.-]{1,254}\.googleapis\.com$")
OPERATION_NAME = re.compile(r"^[A-Za-z0-9._/-]{1,240}$")
PROJECT_TYPE = "gcp.resourcemanager.project"
BILLING_TYPE = "gcp.billing.project_billing_info"
SERVICE_TYPE = "gcp.serviceusage.service"
RESOURCE_MANAGER = "cloudresourcemanager.googleapis.com"
CLOUD_BILLING = "cloudbilling.googleapis.com"
SERVICE_USAGE = "serviceusage.googleapis.com"


class ProjectLifecycleError(RuntimeError):
    """Provider request or exact target could not be safely reconciled."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class GCPProjectLifecycleAdapter:
    """Create one named run project, attach/detach billing, and enable APIs."""

    def __init__(
        self,
        transport: Transport,
        token_source: TokenSource,
        *,
        project_id: str,
        run_id: str,
        parent: str,
        billing_account_name: str,
        enabled_services: list[str],
        identity_transport: IdentityTransport | None = None,
        project_reclaimer_email: str | None = None,
        project_reclaimer_auditor_id: str | None = None,
        timeout: int = 30,
    ):
        if (
            not isinstance(project_id, str)
            or PROJECT_ID.fullmatch(project_id) is None
            or any(word in project_id for word in ("google", "null", "undefined", "ssl"))
        ):
            raise ValueError("an exact run project ID is required")
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            raise ValueError("a valid run ID is required")
        if not isinstance(parent, str) or PARENT.fullmatch(parent) is None:
            raise ValueError("an exact organization or folder parent is required")
        if not isinstance(billing_account_name, str) or not BILLING_ACCOUNT.fullmatch(
            billing_account_name
        ):
            raise ValueError("an exact canonical billing account is required")
        if (
            not isinstance(enabled_services, list)
            or not enabled_services
            or any(not isinstance(item, str) or not API_NAME.fullmatch(item)
                   for item in enabled_services)
            or enabled_services != sorted(set(enabled_services))
        ):
            raise ValueError("enabled services must be a sorted unique API allowlist")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
            raise ValueError("timeout must be between 1 and 60 seconds")
        source_binds_principal = callable(getattr(token_source, "verify_principal", None))
        if not source_binds_principal and (identity_transport is None) != (project_reclaimer_email is None):
            raise ValueError(
                "project reclaimer identity transport and email must be configured together"
            )
        if project_reclaimer_auditor_id is not None and not isinstance(project_reclaimer_auditor_id, str):
            raise ValueError("project reclaimer principal ID must be a string")
        self._transport = transport
        self._token_source = token_source
        self._identity_transport = identity_transport
        self._project_reclaimer_email = project_reclaimer_email
        self._project_reclaimer_auditor_id = project_reclaimer_auditor_id
        self._project_reclaimer_subject = None
        self._project_id = project_id
        self._run_id = run_id
        self._parent = parent
        self._billing_account_name = billing_account_name
        self._enabled_services = frozenset(enabled_services)
        self._timeout = timeout

    @property
    def project_resource_id(self) -> str:
        return f"projects/{self._project_id}"

    def authorize_actor(self, lease_owner, action, resource_type) -> VerifiedGoogleUser | None:
        """Bind retirement lease use to its configured, verified principal."""
        if action != "delete" or resource_type not in {PROJECT_TYPE, BILLING_TYPE}:
            return None
        if lease_owner != "operator:project-reclaimer" and not re.fullmatch(
            r"janitor:google-service-account-[0-9]{1,30}",
            lease_owner if isinstance(lease_owner, str) else "",
        ):
            raise ProjectLifecycleError(
                "project retirement requires the named project-reclaimer lease or per-run janitor lease"
            )
        token = self._access_token()
        principal = self._verify_project_reclaimer(token)
        if lease_owner == "operator:project-reclaimer":
            if not principal.auditor_id.startswith("google-user-"):
                raise ProjectLifecycleError("human project-reclaimer lease requires a verified Google user")
        elif lease_owner != f"janitor:{principal.auditor_id}" or not principal.auditor_id.startswith(
            "google-service-account-"
        ):
            raise ProjectLifecycleError("janitor lease does not match its verified service-account principal")
        return principal

    def submit(self, action, resource_type, resource_id, request) -> ProviderReceipt:
        if not isinstance(request, dict):
            raise ProjectLifecycleError("project operation request must be an object")
        if resource_type == PROJECT_TYPE:
            if resource_id != self.project_resource_id:
                raise ProjectLifecycleError("project operation target does not match this run")
            if action == "delete":
                if request != {}:
                    raise ProjectLifecycleError("project delete request body must be empty")
                return self._delete_project()
            if action != "create":
                raise ProjectLifecycleError("project action must create or delete")
            expected = {
                "project_id": self._project_id,
                "display_name": self._run_id,
                "parent": self._parent,
                "labels": {"lab_run_id": self._run_id},
            }
            if request != expected:
                raise ProjectLifecycleError("project creation request differs from the reviewed run")
            return self._create_project()
        if resource_type == BILLING_TYPE:
            if resource_id != self.project_resource_id:
                raise ProjectLifecycleError("billing operation target does not match this run")
            if action == "create":
                if request != {"billing_account_name": self._billing_account_name}:
                    raise ProjectLifecycleError("billing link must use the reviewed account")
                return self._set_billing(self._billing_account_name)
            if action == "delete":
                if request != {"billing_account_name": ""}:
                    raise ProjectLifecycleError("billing unlink must clear the account name")
                return self._set_billing("", require_project_reclaimer=True)
            raise ProjectLifecycleError("billing action must create or delete")
        if resource_type == SERVICE_TYPE:
            match = re.fullmatch(
                rf"projects/{re.escape(self._project_id)}/services/([a-z][a-z0-9.-]+\.googleapis\.com)",
                resource_id,
            )
            # The run contract uses an exact service resource ID, rather than
            # the bare project ID, for Service Usage operations.
            if match is None:
                raise ProjectLifecycleError("service operation target is malformed")
            service = match.group(1)
            if service not in self._enabled_services or action != "create" or request != {}:
                raise ProjectLifecycleError("service is outside the reviewed enablement allowlist")
            return self._enable_service(service)
        raise ProjectLifecycleError("project lifecycle resource type is unsupported")

    def inspect_resource_for_action(
        self, action, resource_type, resource_id
    ) -> ProviderReceipt:
        require_project_reclaimer = (
            action == "delete" and resource_type in {PROJECT_TYPE, BILLING_TYPE}
        )
        return self._inspect_resource(
            resource_type, resource_id,
            require_project_reclaimer=require_project_reclaimer,
        )

    def inspect_resource(self, resource_type, resource_id) -> ProviderReceipt:
        return self._inspect_resource(resource_type, resource_id)

    def _inspect_resource(
        self, resource_type, resource_id, *, require_project_reclaimer=False
    ) -> ProviderReceipt:
        if resource_type == PROJECT_TYPE:
            if resource_id != self.project_resource_id:
                raise ProjectLifecycleError("project inspection target does not match this run")
            status, payload = self._send(
                "GET", RESOURCE_MANAGER, f"/v3/{self.project_resource_id}",
                require_project_reclaimer=require_project_reclaimer,
            )
            if status == 404:
                return ProviderReceipt("absent", _digest({"status": 404}))
            if status != 200 or not isinstance(payload, dict):
                raise ProjectLifecycleError(f"project inspection returned HTTP {status}")
            labels = payload.get("labels")
            owned = (
                payload.get("projectId") == self._project_id
                and payload.get("displayName") == self._run_id
                and payload.get("parent") == self._parent
                and isinstance(labels, dict)
                and labels.get("lab_run_id") == self._run_id
            )
            state = (
                "present" if owned and payload.get("state") == "ACTIVE"
                else "retiring" if owned and payload.get("state") == "DELETE_REQUESTED"
                else "unknown"
            )
            operation_name = None
            if state == "retiring":
                operation_name = (
                    f"resourcemanager-delete/projects/{self._project_id}/operations/accepted"
                )
            return ProviderReceipt(state, _digest(payload), operation_name)
        if resource_type == BILLING_TYPE:
            if resource_id != self.project_resource_id:
                raise ProjectLifecycleError("billing inspection target does not match this run")
            status, payload = self._send(
                "GET", CLOUD_BILLING,
                f"/v1/{self.project_resource_id}/billingInfo",
                require_project_reclaimer=require_project_reclaimer,
            )
            if status == 404:
                return ProviderReceipt("absent", _digest({"status": 404}))
            if status != 200 or not isinstance(payload, dict):
                raise ProjectLifecycleError(f"billing inspection returned HTTP {status}")
            account = payload.get("billingAccountName", "")
            enabled = payload.get("billingEnabled") is True
            if enabled and account == self._billing_account_name:
                state = "present"
            elif not enabled and account == "":
                state = "absent"
            else:
                state = "unknown"
            return ProviderReceipt(state, _digest(payload))
        if resource_type == SERVICE_TYPE:
            service = self._service_from_id(resource_id)
            status, payload = self._send(
                "GET", SERVICE_USAGE,
                f"/v1/projects/{self._project_id}/services/{service}",
            )
            if status == 404:
                return ProviderReceipt("absent", _digest({"status": 404}))
            if status != 200 or not isinstance(payload, dict):
                raise ProjectLifecycleError(f"service inspection returned HTTP {status}")
            if payload.get("name") != f"projects/{self._project_id}/services/{service}":
                return ProviderReceipt("unknown", _digest(payload))
            service_state = payload.get("state")
            state = (
                "present" if service_state == "ENABLED"
                else "absent" if service_state == "DISABLED"
                else "unknown"
            )
            return ProviderReceipt(state, _digest(payload))
        raise ProjectLifecycleError("project lifecycle resource type is unsupported")

    def poll_operation(self, operation_name: str) -> ProviderReceipt:
        if not isinstance(operation_name, str) or not OPERATION_NAME.fullmatch(operation_name):
            raise ProjectLifecycleError("provider operation name is malformed")
        delete_prefix = f"resourcemanager-delete/projects/{self._project_id}/operations/"
        if operation_name.startswith(delete_prefix):
            provider_name = operation_name[len(delete_prefix):]
            if provider_name == "accepted":
                return self._inspect_project_deletion(operation_name)
            if re.fullmatch(r"[A-Za-z0-9._~-]{1,180}", provider_name) is None:
                raise ProjectLifecycleError("provider operation name is malformed")
            status, payload = self._send(
                "GET", RESOURCE_MANAGER,
                f"/v3/operations/{quote(provider_name, safe='._~-')}",
                require_project_reclaimer=True,
            )
            if status == 404:
                raise ProjectLifecycleError("provider operation is no longer observable")
            if status != 200 or not isinstance(payload, dict):
                raise ProjectLifecycleError(f"provider operation poll returned HTTP {status}")
            if payload.get("name") != f"operations/{provider_name}":
                raise ProjectLifecycleError("provider returned a different operation identity")
            receipt = self._operation_receipt(
                payload, operation_name, RESOURCE_MANAGER, operation_kind="delete"
            )
            if receipt.state != "succeeded":
                return receipt
            return self._inspect_project_deletion(operation_name)
        if operation_name.startswith(f"resourcemanager/projects/{self._project_id}/operations/"):
            provider_name = operation_name.split("/operations/", 1)[1]
            if re.fullmatch(r"[A-Za-z0-9._~-]{1,180}", provider_name) is None:
                raise ProjectLifecycleError("provider operation name is malformed")
            host = RESOURCE_MANAGER
            path = f"/v3/operations/{quote(provider_name, safe='._~-')}"
        elif operation_name.startswith(f"serviceusage/projects/{self._project_id}/services/"):
            remainder = operation_name.split("/services/", 1)[1]
            service, separator, provider_name = remainder.partition("/operations/")
            if (
                not separator or service not in self._enabled_services
                or re.fullmatch(r"[A-Za-z0-9._~-]{1,180}", provider_name) is None
            ):
                raise ProjectLifecycleError("provider operation is outside this service allowlist")
            host = SERVICE_USAGE
            path = f"/v1/operations/{quote(provider_name, safe='._~-')}"
        else:
            raise ProjectLifecycleError("provider operation is outside this run")
        status, payload = self._send("GET", host, path)
        if status == 404:
            raise ProjectLifecycleError("provider operation is no longer observable")
        if status != 200 or not isinstance(payload, dict):
            raise ProjectLifecycleError(f"provider operation poll returned HTTP {status}")
        if payload.get("name") != f"operations/{provider_name}":
            raise ProjectLifecycleError("provider returned a different operation identity")
        return self._operation_receipt(payload, operation_name, host)

    def _create_project(self) -> ProviderReceipt:
        body = {
            "projectId": self._project_id,
            "displayName": self._run_id,
            "parent": self._parent,
            "labels": {"lab_run_id": self._run_id},
        }
        status, payload = self._send("POST", RESOURCE_MANAGER, "/v3/projects", body)
        if status not in {200, 201, 202} or not isinstance(payload, dict):
            raise ProjectLifecycleError(f"project creation returned HTTP {status}")
        return self._start_operation(payload, RESOURCE_MANAGER, "resourcemanager")

    def _delete_project(self) -> ProviderReceipt:
        status, project = self._send(
            "GET", RESOURCE_MANAGER, f"/v3/{self.project_resource_id}",
            require_project_reclaimer=True,
        )
        if status == 404:
            return ProviderReceipt("absent", _digest({"status": 404}))
        if status != 200 or not isinstance(project, dict):
            raise ProjectLifecycleError(
                f"project pre-delete inspection returned HTTP {status}"
            )
        labels = project.get("labels")
        owned = (
            project.get("projectId") == self._project_id
            and project.get("displayName") == self._run_id
            and project.get("parent") == self._parent
            and isinstance(labels, dict)
            and labels.get("lab_run_id") == self._run_id
        )
        if not owned:
            raise ProjectLifecycleError("project pre-delete identity does not match this run")
        if project.get("state") == "DELETE_REQUESTED":
            accepted_name = (
                f"resourcemanager-delete/projects/{self._project_id}/operations/accepted"
            )
            return ProviderReceipt("running", _digest(project), accepted_name)
        if project.get("state") != "ACTIVE":
            raise ProjectLifecycleError("project is not active for exact project deletion")

        billing_status, billing = self._send(
            "GET", CLOUD_BILLING,
            f"/v1/{self.project_resource_id}/billingInfo",
            require_project_reclaimer=True,
        )
        if (
            billing_status != 200 or not isinstance(billing, dict)
            or billing.get("projectId") != self._project_id
            or billing.get("billingEnabled") is not False
            or billing.get("billingAccountName", "") != ""
        ):
            raise ProjectLifecycleError(
                "project deletion requires verified detached billing"
            )

        status, payload = self._send(
            "DELETE", RESOURCE_MANAGER, f"/v3/{self.project_resource_id}",
            require_project_reclaimer=True,
        )
        if status not in {200, 202} or not isinstance(payload, dict):
            raise ProjectLifecycleError(f"project deletion returned HTTP {status}")
        return self._start_operation(
            payload, RESOURCE_MANAGER, "resourcemanager-delete", operation_kind="delete"
        )

    def _set_billing(
        self, billing_account_name: str, *, require_project_reclaimer=False
    ) -> ProviderReceipt:
        status, payload = self._send(
            "PUT", CLOUD_BILLING,
            f"/v1/{self.project_resource_id}/billingInfo",
            {"billingAccountName": billing_account_name},
            require_project_reclaimer=require_project_reclaimer,
        )
        if status not in {200, 201} or not isinstance(payload, dict):
            raise ProjectLifecycleError(f"billing update returned HTTP {status}")
        enabled = payload.get("billingEnabled") is True
        account = payload.get("billingAccountName", "")
        if billing_account_name:
            state = "succeeded" if enabled and account == billing_account_name else "unknown"
        else:
            state = "succeeded" if not enabled and account == "" else "unknown"
        return ProviderReceipt(state, _digest(payload))

    def _enable_service(self, service: str) -> ProviderReceipt:
        status, payload = self._send(
            "POST", SERVICE_USAGE,
            f"/v1/projects/{self._project_id}/services/{service}:enable",
        )
        if status not in {200, 201, 202} or not isinstance(payload, dict):
            raise ProjectLifecycleError(f"service enable returned HTTP {status}")
        return self._start_operation(payload, SERVICE_USAGE, "serviceusage", service=service)

    def _start_operation(
        self, payload, host: str, prefix: str, *, service: str | None = None,
        operation_kind: str = "create",
    ) -> ProviderReceipt:
        if payload.get("error"):
            return ProviderReceipt("failed", _digest(payload))
        name = payload.get("name")
        if payload.get("done") is True:
            if host == RESOURCE_MANAGER and operation_kind == "create":
                self._validate_created_project(payload.get("response"))
            if operation_kind == "delete":
                accepted_name = (
                    f"resourcemanager-delete/projects/{self._project_id}/operations/accepted"
                )
                return self._inspect_project_deletion(accepted_name)
            return ProviderReceipt("succeeded", _digest(payload))
        if payload.get("done") is not False or not isinstance(name, str):
            raise ProjectLifecycleError("provider returned malformed long-running operation")
        if not name.startswith("operations/"):
            raise ProjectLifecycleError("provider operation name is not canonical")
        operation_id = name[len("operations/"):]
        if not re.fullmatch(r"[A-Za-z0-9._~-]{1,180}", operation_id):
            raise ProjectLifecycleError("provider operation name is malformed")
        if prefix == "resourcemanager-delete":
            scoped_name = (
                f"resourcemanager-delete/projects/{self._project_id}/operations/"
                f"{operation_id}"
            )
        elif host == SERVICE_USAGE:
            scoped_name = (
                f"{prefix}/projects/{self._project_id}/services/{service}/"
                f"operations/{operation_id}"
            )
        else:
            scoped_name = f"{prefix}/projects/{self._project_id}/operations/{operation_id}"
        if len(scoped_name) > 256:
            raise ProjectLifecycleError("scoped provider operation name exceeds the ledger bound")
        return ProviderReceipt("running", _digest(payload), scoped_name)

    def _operation_receipt(
        self, payload, operation_name: str, host: str, *, operation_kind: str = "create"
    ) -> ProviderReceipt:
        if payload.get("error"):
            return ProviderReceipt("failed", _digest(payload), operation_name)
        if payload.get("done") is False:
            return ProviderReceipt("running", _digest(payload), operation_name)
        if payload.get("done") is not True:
            raise ProjectLifecycleError("provider operation has no terminal state")
        if host == RESOURCE_MANAGER and operation_kind == "create":
            self._validate_created_project(payload.get("response"))
        return ProviderReceipt("succeeded", _digest(payload), operation_name)

    def _inspect_project_deletion(self, operation_name: str) -> ProviderReceipt:
        status, payload = self._send(
            "GET", RESOURCE_MANAGER, f"/v3/{self.project_resource_id}",
            require_project_reclaimer=True,
        )
        if status == 404:
            return ProviderReceipt("succeeded", _digest({"status": 404}), operation_name)
        if status != 200 or not isinstance(payload, dict):
            raise ProjectLifecycleError(
                f"project deletion reconciliation returned HTTP {status}"
            )
        labels = payload.get("labels")
        owned = (
            payload.get("projectId") == self._project_id
            and payload.get("displayName") == self._run_id
            and payload.get("parent") == self._parent
            and isinstance(labels, dict)
            and labels.get("lab_run_id") == self._run_id
        )
        if not owned:
            return ProviderReceipt("unknown", _digest(payload), operation_name)
        if payload.get("state") == "DELETE_REQUESTED":
            return ProviderReceipt("running", _digest(payload), operation_name)
        return ProviderReceipt("unknown", _digest(payload), operation_name)

    def _validate_created_project(self, project: object) -> None:
        if not isinstance(project, dict):
            raise ProjectLifecycleError("project creation result lacks a project resource")
        labels = project.get("labels")
        if (
            project.get("projectId") != self._project_id
            or project.get("displayName") != self._run_id
            or project.get("parent") != self._parent
            or not isinstance(labels, dict)
            or labels.get("lab_run_id") != self._run_id
        ):
            raise ProjectLifecycleError("project creation result does not match the exact run")

    def _service_from_id(self, resource_id: str) -> str:
        match = re.fullmatch(
            rf"projects/{re.escape(self._project_id)}/services/([a-z][a-z0-9.-]+\.googleapis\.com)",
            resource_id if isinstance(resource_id, str) else "",
        )
        if match is None or match.group(1) not in self._enabled_services:
            raise ProjectLifecycleError("service target is outside this run allowlist")
        return match.group(1)

    def _send(
        self, method: str, host: str, path: str, body=None, *,
        require_project_reclaimer=False,
    ):
        parsed = urlsplit(path)
        if (
            host not in {RESOURCE_MANAGER, CLOUD_BILLING, SERVICE_USAGE}
            or not parsed.path.startswith("/")
            or parsed.query or parsed.fragment
            or any(part in {"", ".", ".."} for part in parsed.path.split("/")[1:])
        ):
            raise ProjectLifecycleError("provider request target failed URL validation")
        token = self._access_token()
        if require_project_reclaimer:
            self._verify_project_reclaimer(token)
        try:
            response = self._transport.request(
                method,
                f"https://{host}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    **({"Content-Type": "application/json"} if body is not None else {}),
                },
                body=_canonical(body) if body is not None else None,
                timeout=self._timeout,
                allow_redirects=False,
            )
        except Exception:
            raise ProjectLifecycleError("GCP project lifecycle request failed") from None
        if not 200 <= response.status_code < 300:
            return response.status_code, None
        try:
            payload = response.json()
        except Exception:
            raise ProjectLifecycleError("GCP project lifecycle response was malformed") from None
        return response.status_code, payload

    def _access_token(self) -> str:
        try:
            token = self._token_source.access_token()
        except Exception:
            raise ProjectLifecycleError("short-lived access token is unavailable") from None
        if (
            not isinstance(token, str) or not token or token.strip() != token
            or "\n" in token or "\r" in token
        ):
            raise ProjectLifecycleError("short-lived access token source returned invalid token")
        return token

    def _verify_project_reclaimer(self, token: str) -> VerifiedGoogleUser:
        source_verifier = getattr(self._token_source, "verify_principal", None)
        if not callable(source_verifier) and (
            self._identity_transport is None or self._project_reclaimer_email is None
        ):
            raise ProjectLifecycleError(
                "project-reclaimer principal verification is not configured"
            )
        try:
            if callable(source_verifier):
                principal = source_verifier(token)
            else:
                principal = verify_access_token_principal(
                    self._identity_transport,
                    token,
                    expected_email=self._project_reclaimer_email,
                )
        except IdentityVerificationError:
            raise ProjectLifecycleError(
                "project-reclaimer Google principal could not be verified"
            ) from None
        if self._project_reclaimer_email is not None and principal.email != self._project_reclaimer_email:
            raise ProjectLifecycleError("project-reclaimer email does not match configured principal")
        if (
            self._project_reclaimer_auditor_id is not None
            and principal.auditor_id != self._project_reclaimer_auditor_id
        ):
            raise ProjectLifecycleError("project-reclaimer subject does not match configured principal")
        if (
            self._project_reclaimer_subject is not None
            and principal.auditor_id != self._project_reclaimer_subject
        ):
            raise ProjectLifecycleError(
                "project-reclaimer Google subject changed during the run"
            )
        self._project_reclaimer_subject = principal.auditor_id
        return principal
