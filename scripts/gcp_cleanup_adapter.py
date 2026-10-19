"""Exact-ID, deletion-only GCP adapter for the current lab resource set.

This adapter makes no credential discovery and cannot create resources. The
caller supplies the run's project ID, run ID, short-lived access-token source,
and an HTTPS transport. It intentionally supports only resource types whose
cleanup semantics are defined in the current GCP lab design.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit
import uuid

from recovery_controller import ProviderReceipt


FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
RUN_ID = re.compile(r"^lab-[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?$")
ZONES = frozenset({"europe-west2-a", "europe-west2-b", "europe-west2-c"})
SUPPORTED_TYPES = frozenset(
    {
        "gcp.compute.network",
        "gcp.compute.subnetwork",
        "gcp.compute.router",
        "gcp.compute.router_nat",
        "gcp.pubsub.topic",
        "gcp.pubsub.subscription",
        "gcp.gke.node_pool",
        "gcp.gke.cluster",
        "gcp.gkehub.feature_membership",
        "gcp.gkehub.membership",
        "gcp.gkehub.feature",
    }
)
GOOGLE_HOSTS = frozenset(
    {
        "compute.googleapis.com",
        "container.googleapis.com",
        "gkehub.googleapis.com",
        "pubsub.googleapis.com",
        "www.googleapis.com",
    }
)


class CleanupAdapterError(RuntimeError):
    """Provider request or exact target could not be safely reconciled."""


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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class _HTTPResponse:
    status_code: int
    payload: bytes

    def json(self) -> object:
        if not self.payload:
            return {}
        return json.loads(self.payload)


class UrllibTransport:
    """Small HTTPS transport that refuses redirects and hides response text."""

    def __init__(self):
        self._opener = build_opener(_NoRedirect())

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict,
        body: bytes | None,
        timeout: int,
        allow_redirects: bool,
    ) -> _HTTPResponse:
        del allow_redirects  # redirects are never followed by this transport
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return _HTTPResponse(response.status, response.read())
        except HTTPError as exc:
            # Keep only status; provider error bodies can contain sensitive data.
            return _HTTPResponse(exc.code, b"")
        except (URLError, TimeoutError, OSError):
            raise CleanupAdapterError("GCP provider request failed") from None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _segments(resource_id: str, resource_type: str) -> tuple[str, ...]:
    if not isinstance(resource_id, str) or not resource_id:
        raise CleanupAdapterError("resource ID must be a non-empty string")
    parsed = urlsplit(resource_id)
    if parsed.scheme:
        if parsed.scheme != "https" or parsed.hostname not in GOOGLE_HOSTS:
            raise CleanupAdapterError("resource ID is outside the Google API allowlist")
        try:
            has_unsupported_authority = bool(
                parsed.username or parsed.password or parsed.port
            )
        except ValueError:
            raise CleanupAdapterError("resource ID contains an invalid URL port") from None
        if has_unsupported_authority or parsed.query or parsed.fragment:
            raise CleanupAdapterError("resource ID contains unsupported URL components")
        path = parsed.path
        if parsed.hostname == "www.googleapis.com":
            if not path.startswith("/compute/v1/"):
                raise CleanupAdapterError("Compute resource ID has an unsupported API path")
            path = path[len("/compute/v1/"):]
        elif parsed.hostname == "compute.googleapis.com":
            if not path.startswith("/compute/v1/"):
                raise CleanupAdapterError("Compute resource ID has an unsupported API path")
            path = path[len("/compute/v1/"):]
        elif parsed.hostname == "container.googleapis.com":
            if not path.startswith("/v1/"):
                raise CleanupAdapterError("GKE resource ID has an unsupported API path")
            path = path[len("/v1/"):]
        elif parsed.hostname == "pubsub.googleapis.com":
            if not path.startswith("/v1/"):
                raise CleanupAdapterError("Pub/Sub resource ID has an unsupported API path")
            path = path[len("/v1/"):]
        else:
            version = next(
                (v for v in ("v2", "v1alpha", "v1") if path.startswith(f"/{v}/")),
                None,
            )
            if version is None:
                raise CleanupAdapterError("Fleet resource ID has an unsupported API path")
            path = path[len(version) + 2:]
    else:
        path = resource_id.lstrip("/")
    parts = tuple(path.split("/"))
    if any(
        not item or item in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9._~-]+", item)
        for item in parts
    ):
        raise CleanupAdapterError("resource ID path is malformed")
    if len(parts) < 4 or parts[0] != "projects":
        raise CleanupAdapterError("resource ID must be a canonical project resource name")
    return parts


def _resource_path(resource_type: str, resource_id: str, project_id: str, run_id: str):
    if resource_type not in SUPPORTED_TYPES:
        raise CleanupAdapterError("resource type has no qualified cleanup mapping")
    parts = _segments(resource_id, resource_type)
    if parts[1] != project_id:
        raise CleanupAdapterError("resource ID is outside the exact run project")
    if resource_type == "gcp.gkehub.feature":
        if parts != ("projects", project_id, "locations", "global", "features", "configmanagement"):
            raise CleanupAdapterError("only the run project's Config Sync Feature is supported")
    elif not any(run_id in item for item in parts[2:]):
        raise CleanupAdapterError("resource ID does not identify this run")

    if resource_type == "gcp.compute.network":
        if len(parts) != 5 or parts[2:4] != ("global", "networks"):
            raise CleanupAdapterError("network resource name is malformed")
        return "compute.googleapis.com", f"/compute/v1/{'/'.join(parts)}", "compute-global"
    if resource_type == "gcp.compute.subnetwork":
        if len(parts) != 6 or parts[2] != "regions" or parts[3] != "europe-west2" or parts[4] != "subnetworks":
            raise CleanupAdapterError("subnetwork resource name is malformed")
        return "compute.googleapis.com", f"/compute/v1/{'/'.join(parts)}", "compute-region"
    if resource_type == "gcp.compute.router":
        if len(parts) != 6 or parts[2] != "regions" or parts[3] != "europe-west2" or parts[4] != "routers":
            raise CleanupAdapterError("router resource name is malformed")
        return "compute.googleapis.com", f"/compute/v1/{'/'.join(parts)}", "compute-region"
    if resource_type == "gcp.compute.router_nat":
        if len(parts) != 8 or parts[2] != "regions" or parts[3] != "europe-west2" or parts[4] != "routers" or parts[6] != "nats":
            raise CleanupAdapterError("router NAT resource name is malformed")
        router = "/".join(parts[:6])
        return "compute.googleapis.com", f"/compute/v1/{router}", parts[7]
    if resource_type == "gcp.pubsub.topic":
        if len(parts) != 4 or parts[2] != "topics":
            raise CleanupAdapterError("Pub/Sub topic resource name is malformed")
        return "pubsub.googleapis.com", f"/v1/{'/'.join(parts)}", "pubsub"
    if resource_type == "gcp.pubsub.subscription":
        if len(parts) != 4 or parts[2] != "subscriptions":
            raise CleanupAdapterError("Pub/Sub subscription resource name is malformed")
        return "pubsub.googleapis.com", f"/v1/{'/'.join(parts)}", "pubsub"
    if resource_type == "gcp.gke.cluster":
        if len(parts) != 6 or parts[2] != "locations" or parts[3] != "europe-west2" or parts[4] != "clusters":
            raise CleanupAdapterError("GKE cluster resource name is malformed")
        return "container.googleapis.com", f"/v1/{'/'.join(parts)}", "container"
    if resource_type == "gcp.gke.node_pool":
        if len(parts) != 8 or parts[2] != "locations" or parts[3] != "europe-west2" or parts[4] != "clusters" or parts[6] != "nodePools":
            raise CleanupAdapterError("GKE node pool resource name is malformed")
        return "container.googleapis.com", f"/v1/{'/'.join(parts)}", "container"
    if resource_type == "gcp.gkehub.feature_membership":
        if len(parts) != 8 or parts[2:4] != ("locations", "global") or parts[4] != "memberships" or parts[6] != "features":
            raise CleanupAdapterError("Fleet membership feature resource name is malformed")
        return "gkehub.googleapis.com", f"/v2/{'/'.join(parts)}", "gkehub"
    if resource_type == "gcp.gkehub.membership":
        if len(parts) != 6 or parts[2:4] != ("locations", "global") or parts[4] != "memberships":
            raise CleanupAdapterError("Fleet membership resource name is malformed")
        return "gkehub.googleapis.com", f"/v1/{'/'.join(parts)}", "gkehub"
    if resource_type == "gcp.gkehub.feature":
        return "gkehub.googleapis.com", f"/v1alpha/{'/'.join(parts)}", "gkehub"
    raise CleanupAdapterError("resource type has no qualified cleanup mapping")


class GCPCleanupAdapter:
    """Delete and inspect exact run-owned resources; creation is prohibited."""

    def __init__(
        self,
        transport: Transport,
        token_source: TokenSource,
        *,
        project_id: str,
        run_id: str,
        cluster_delete_authorizer: Callable[[str], bool] | None = None,
        timeout: int = 30,
    ):
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise ValueError("an exact run project ID is required")
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("a valid run ID is required")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
            raise ValueError("timeout must be between 1 and 60 seconds")
        self._transport = transport
        self._token_source = token_source
        self._project_id = project_id
        self._run_id = run_id
        self._cluster_delete_authorizer = cluster_delete_authorizer
        self._timeout = timeout

    def submit(self, action, resource_type, resource_id, request) -> ProviderReceipt:
        if action != "delete":
            raise CleanupAdapterError("cleanup adapter cannot create or update resources")
        if request != {}:
            raise CleanupAdapterError("cleanup operation does not accept a request body")
        host, path, discriminator = _resource_path(
            resource_type, resource_id, self._project_id, self._run_id
        )
        if resource_type == "gcp.compute.router_nat":
            return self._remove_nat(path, discriminator)
        if resource_type == "gcp.gke.cluster":
            if self._cluster_delete_authorizer is None:
                raise CleanupAdapterError(
                    "GKE cluster deletion requires a separate protection review"
                )
            try:
                approved = self._cluster_delete_authorizer(resource_id)
            except Exception:
                raise CleanupAdapterError(
                    "GKE cluster deletion protection review failed"
                ) from None
            if approved is not True:
                raise CleanupAdapterError(
                    "GKE cluster deletion protection review did not authorize this exact cluster"
                )
        query = ""
        if host == "compute.googleapis.com":
            request_id = uuid.uuid5(uuid.NAMESPACE_URL, resource_id)
            query = f"?requestId={request_id}"
        response, payload = self._send("DELETE", host, path + query)
        if response.status_code == 404:
            return ProviderReceipt("absent", _fingerprint({"status": 404}))
        if response.status_code not in {200, 202}:
            raise CleanupAdapterError(
                f"provider delete returned HTTP {response.status_code}"
            )
        if not payload:
            return ProviderReceipt("succeeded", _fingerprint({"status": response.status_code}))
        if not isinstance(payload, dict):
            raise CleanupAdapterError("provider delete returned malformed JSON")
        if not payload:
            return ProviderReceipt("succeeded", _fingerprint(payload))
        api_version = "v1" if host == "compute.googleapis.com" else self._api_version(path)
        receipt = self._operation_receipt(host, payload, api_version=api_version)
        if receipt.state == "running":
            self._operation_path(receipt.operation_name, self._project_id)
        return receipt

    def inspect_resource(self, resource_type, resource_id) -> ProviderReceipt:
        host, path, discriminator = _resource_path(
            resource_type, resource_id, self._project_id, self._run_id
        )
        response, payload = self._send("GET", host, path)
        if response.status_code == 404:
            return ProviderReceipt("absent", _fingerprint({"status": 404}))
        if response.status_code != 200 or not isinstance(payload, dict):
            raise CleanupAdapterError(
                f"provider inspection returned HTTP {response.status_code}"
            )
        if resource_type == "gcp.compute.router_nat":
            nats = payload.get("nats")
            if not isinstance(nats, list):
                raise CleanupAdapterError("router response has no complete NAT configuration")
            matches = [item for item in nats if isinstance(item, dict) and item.get("name") == discriminator]
            if len(matches) > 1:
                raise CleanupAdapterError("router contains ambiguous matching NAT entries")
            state = "present" if matches else "absent"
        else:
            state = "present"
        return ProviderReceipt(state, _fingerprint(payload))

    def poll_operation(self, operation_name) -> ProviderReceipt:
        if not isinstance(operation_name, str):
            raise CleanupAdapterError("provider operation name is malformed")
        host, path = self._operation_path(operation_name, self._project_id)
        response, payload = self._send("GET", host, path)
        if response.status_code == 404:
            raise CleanupAdapterError("provider operation is no longer observable")
        if response.status_code != 200 or not isinstance(payload, dict):
            raise CleanupAdapterError(
                f"provider operation poll returned HTTP {response.status_code}"
            )
        return self._operation_receipt(
            host, payload, operation_name=operation_name,
            api_version="v1" if host == "compute.googleapis.com" else self._api_version(path),
        )

    def _remove_nat(self, router_path: str, nat_name: str) -> ProviderReceipt:
        response, router = self._send("GET", "compute.googleapis.com", router_path)
        if response.status_code == 404:
            return ProviderReceipt("absent", _fingerprint({"status": 404}))
        if response.status_code != 200 or not isinstance(router, dict):
            raise CleanupAdapterError("router inspection failed before NAT update")
        nats = router.get("nats")
        fingerprint = router.get("fingerprint")
        if (
            not isinstance(nats, list)
            or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in nats)
            or not isinstance(fingerprint, str)
            or not fingerprint
        ):
            raise CleanupAdapterError("router response lacks NAT list or concurrency fingerprint")
        matches = [item for item in nats if item.get("name") == nat_name]
        if len(matches) > 1:
            raise CleanupAdapterError("router contains ambiguous matching NAT entries")
        if not matches:
            return ProviderReceipt("absent", _fingerprint(router))
        remaining = [item for item in nats if item.get("name") != nat_name]
        body = _canonical({"fingerprint": fingerprint, "nats": remaining})
        response, payload = self._send(
            "PATCH", "compute.googleapis.com", router_path, body=body
        )
        if response.status_code == 404:
            return ProviderReceipt("absent", _fingerprint({"status": 404}))
        if response.status_code not in {200, 202}:
            raise CleanupAdapterError(f"router NAT patch returned HTTP {response.status_code}")
        if not isinstance(payload, dict):
            raise CleanupAdapterError("router NAT patch returned malformed JSON")
        receipt = self._operation_receipt(
            "compute.googleapis.com", payload, api_version="v1"
        )
        if receipt.state == "running":
            self._operation_path(receipt.operation_name, self._project_id)
        return receipt

    def _send(self, method, host, path, *, body=None):
        parsed_target = urlsplit(path)
        path_parts = parsed_target.path.split("/")
        if (
            host not in GOOGLE_HOSTS
            or not parsed_target.path.startswith("/")
            or parsed_target.fragment
            or (
                parsed_target.query
                and (
                    method != "DELETE"
                    or host != "compute.googleapis.com"
                    or not re.fullmatch(
                        r"requestId=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        parsed_target.query,
                    )
                )
            )
            or any(
                not part or part in {".", ".."}
                or not re.fullmatch(r"[A-Za-z0-9._~-]+", part)
                for part in path_parts[1:]
            )
        ):
            raise CleanupAdapterError("provider request target failed URL validation")
        try:
            token = self._token_source.access_token()
        except Exception:
            raise CleanupAdapterError("short-lived access token is unavailable") from None
        if (
            not isinstance(token, str) or not token or token.strip() != token
            or "\n" in token or "\r" in token
        ):
            raise CleanupAdapterError("short-lived access token source returned invalid token")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = f"https://{host}{path}"
        try:
            response = self._transport.request(
                method,
                url,
                headers=headers,
                body=body,
                timeout=self._timeout,
                allow_redirects=False,
            )
        except CleanupAdapterError:
            raise
        except Exception:
            raise CleanupAdapterError("GCP provider request failed") from None
        if not 200 <= response.status_code < 300:
            return response, None
        try:
            payload = response.json()
        except Exception:
            raise CleanupAdapterError("GCP provider response was malformed") from None
        return response, payload

    @staticmethod
    def _operation_receipt(host, payload, operation_name=None, api_version="v1"):
        if payload.get("error"):
            return ProviderReceipt(
                "failed", _fingerprint(payload), operation_name
            )
        status = payload.get("status")
        done = payload.get("done")
        if status == "DONE" or done is True:
            return ProviderReceipt("succeeded", _fingerprint(payload), operation_name)
        if status == "RUNNING" or done is False:
            name = operation_name or GCPCleanupAdapter._normalized_operation_name(
                host, payload, api_version=api_version
            )
            return ProviderReceipt("running", _fingerprint(payload), name)
        raise CleanupAdapterError("provider returned an unrecognized operation state")

    @staticmethod
    def _normalized_operation_name(host, payload, *, api_version):
        name = payload.get("name")
        scope = payload.get("region") or payload.get("zone")
        if host == "container.googleapis.com":
            self_link = payload.get("selfLink")
            parsed = urlsplit(self_link) if isinstance(self_link, str) else None
            path = parsed.path if parsed and parsed.hostname == host else ""
            if not path.startswith("/v1/"):
                raise CleanupAdapterError("GKE operation has no canonical name")
            return f"container/{path[len('/v1/'):]}"
        if host == "gkehub.googleapis.com":
            if not isinstance(name, str) or not name.startswith("projects/"):
                raise CleanupAdapterError("Fleet operation has no canonical name")
            return f"gkehub-{api_version}/{name}"
        if not isinstance(name, str) or not name:
            raise CleanupAdapterError("Compute operation has no name")
        if scope is not None and not isinstance(scope, str):
            raise CleanupAdapterError("Compute operation scope is malformed")
        scope_type = (
            "regions" if isinstance(scope, str) and "/regions/" in scope
            else "zones" if isinstance(scope, str) and "/zones/" in scope
            else "global"
        )
        scope_value = scope.rstrip("/").split("/")[-1] if scope_type != "global" else None
        project_url = payload.get("selfLink", "")
        match = re.search(r"/projects/([^/]+)/", project_url)
        project = match.group(1) if match else None
        if not project:
            target_link = payload.get("targetLink", "")
            match = re.search(r"/projects/([^/]+)/", target_link)
            project = match.group(1) if match else None
        if not project:
            raise CleanupAdapterError("Compute operation does not identify its project")
        prefix = f"projects/{project}/{scope_type}/"
        scoped = f"{scope_value}/" if scope_value is not None else ""
        return f"compute/{prefix}{scoped}operations/{name}"

    @staticmethod
    def _api_version(path):
        parts = path.split("/")
        if len(parts) > 2 and parts[1:3] == ["compute", "v1"]:
            return "v1"
        if len(parts) > 1 and parts[1] in {"v1", "v2", "v1alpha"}:
            return parts[1]
        raise CleanupAdapterError("provider API version is not recognized")

    @staticmethod
    def _operation_path(operation_name, project_id):
        if operation_name.startswith("container/projects/"):
            path = operation_name[len("container/"):]
            parts = path.split("/")
            if (
                len(parts) != 6 or parts[0] != "projects"
                or parts[1] != project_id or parts[2] != "locations"
                or parts[3] != "europe-west2" or parts[4] != "operations"
            ):
                raise CleanupAdapterError("GKE operation is outside the run project and region")
            return "container.googleapis.com", f"/v1/{operation_name[len('container/'):]}"
        if operation_name.startswith("gkehub-v1/projects/"):
            if not operation_name.startswith(f"gkehub-v1/projects/{project_id}/locations/global/operations/"):
                raise CleanupAdapterError("Fleet operation is outside the run project")
            return "gkehub.googleapis.com", f"/v1/{operation_name[len('gkehub-v1/'):]}"
        if operation_name.startswith("gkehub-v2/projects/"):
            if not operation_name.startswith(f"gkehub-v2/projects/{project_id}/locations/global/operations/"):
                raise CleanupAdapterError("Fleet operation is outside the run project")
            return "gkehub.googleapis.com", f"/v2/{operation_name[len('gkehub-v2/'):]}"
        if operation_name.startswith("gkehub-v1alpha/projects/"):
            if not operation_name.startswith(f"gkehub-v1alpha/projects/{project_id}/locations/global/operations/"):
                raise CleanupAdapterError("Fleet operation is outside the run project")
            return "gkehub.googleapis.com", f"/v1alpha/{operation_name[len('gkehub-v1alpha/'):]}"
        if operation_name.startswith("compute/projects/"):
            parts = operation_name[len("compute/"):].split("/")
            if len(parts) == 5 and parts[2] == "global" and parts[3] == "operations":
                if parts[1] != project_id:
                    raise CleanupAdapterError("Compute operation is outside the run project")
                return "compute.googleapis.com", f"/compute/v1/{'/'.join(parts)}"
            if len(parts) == 6 and parts[2] == "regions" and parts[4] == "operations":
                if parts[1] != project_id or parts[3] != "europe-west2":
                    raise CleanupAdapterError("Compute operation is outside the run project and region")
                return "compute.googleapis.com", f"/compute/v1/{'/'.join(parts)}"
            if len(parts) == 6 and parts[2] == "zones" and parts[4] == "operations":
                if parts[1] != project_id or parts[3] not in ZONES:
                    raise CleanupAdapterError("Compute operation is outside the run project and zones")
                return "compute.googleapis.com", f"/compute/v1/{'/'.join(parts)}"
        raise CleanupAdapterError("provider operation name is outside the supported APIs")
