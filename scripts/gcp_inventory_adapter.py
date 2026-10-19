"""Read-only, paginated GCP inventory collector for independent run audits.

The adapter accepts an already-authenticated transport and never obtains
credentials, mutates resources, or discovers projects outside the one supplied.
It emits the normalized workload report consumed by ``independent_auditor``.
Provider access must be stopped on any failed or ambiguous page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Protocol
from urllib.parse import quote, urlsplit

from independent_auditor import (
    AUDIT_SCHEMA,
    WORKLOAD_SURFACES,
    manifest_fingerprint,
)
from gcp_token_identity import (
    IdentityVerificationError,
    PRINCIPAL_EMAIL,
    verify_access_token_principal,
)


class Response(Protocol):
    status_code: int

    def json(self) -> object: ...


class ReadOnlyTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        params: dict,
        headers: dict,
        timeout: int,
        allow_redirects: bool,
    ) -> Response: ...


class InventoryCollectionError(RuntimeError):
    """A provider response cannot prove complete inventory coverage."""


MAX_PAGES = 10_000
GOOGLE_API_HOSTS = frozenset(
    {
        "compute.googleapis.com",
        "container.googleapis.com",
        "gkehub.googleapis.com",
        "pubsub.googleapis.com",
        "storage.googleapis.com",
    }
)


@dataclass(frozen=True)
class Collection:
    resources: list[dict]
    pages: int
    response_sha256: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _response_pages(
    transport: ReadOnlyTransport,
    url: str,
    *,
    token: str,
    params: dict | None = None,
    collection_key: str = "items",
    aggregated: bool = False,
    aggregated_resource_key: str | None = None,
) -> Collection:
    """Read every page; reject partial scopes, malformed pages and token loops."""
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in GOOGLE_API_HOSTS:
        raise InventoryCollectionError(
            "inventory URL is outside the Google API allowlist"
        )
    query = dict(params or {})
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    next_token = None
    seen_tokens = set()
    page_bodies = []
    resources = []
    pages = 0
    while True:
        if next_token is not None:
            if next_token in seen_tokens:
                raise InventoryCollectionError("provider pagination token repeated")
            seen_tokens.add(next_token)
            query["pageToken"] = next_token
        try:
            response = transport.get(
                url,
                params=query,
                headers=headers,
                timeout=30,
                allow_redirects=False,
            )
        except Exception:
            raise InventoryCollectionError(
                "provider inventory request failed"
            ) from None
        if response.status_code != 200:
            raise InventoryCollectionError(
                f"provider inventory request returned HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except Exception:
            raise InventoryCollectionError("provider response was not JSON") from None
        if not isinstance(body, dict):
            raise InventoryCollectionError("provider page must be a JSON object")
        if body.get("unreachable") or body.get("unreachables"):
            raise InventoryCollectionError(
                "provider reported an unreachable inventory scope"
            )
        if body.get("warnings"):
            raise InventoryCollectionError(
                "provider reported incomplete inventory warnings"
            )
        warning = body.get("warning")
        if warning:
            code = warning.get("code") if isinstance(warning, dict) else None
            if code != "NO_RESULTS_ON_PAGE":
                raise InventoryCollectionError("provider reported an incomplete page")
        page_bodies.append(body)
        pages += 1
        if pages > MAX_PAGES:
            raise InventoryCollectionError(
                "provider pagination exceeded the safety limit"
            )
        entries = body.get(collection_key)
        if aggregated:
            if entries is None:
                entries = {}
            if not isinstance(entries, dict):
                raise InventoryCollectionError(
                    "aggregated provider page has invalid scopes"
                )
            for scope in entries.values():
                if not isinstance(scope, dict):
                    raise InventoryCollectionError(
                        "aggregated provider scope is malformed"
                    )
                warning = scope.get("warning")
                if warning:
                    code = warning.get("code") if isinstance(warning, dict) else None
                    if code != "NO_RESULTS_ON_PAGE":
                        raise InventoryCollectionError(
                            "provider reported a partial scope"
                        )
                    resources_for_scope = scope.get(
                        aggregated_resource_key or collection_key
                    )
                    if resources_for_scope not in (None, []):
                        raise InventoryCollectionError(
                            "empty-scope warning conflicts with returned resources"
                        )
                    continue
                scoped_items = scope.get(aggregated_resource_key or collection_key)
                if scoped_items is None:
                    continue
                if not isinstance(scoped_items, list):
                    raise InventoryCollectionError(
                        "aggregated resource list is malformed"
                    )
                resources.extend(scoped_items)
        else:
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise InventoryCollectionError("provider resource list is malformed")
            resources.extend(entries)
        next_token = body.get("nextPageToken")
        if next_token is None or next_token == "":
            break
        if not isinstance(next_token, str):
            raise InventoryCollectionError("provider pagination token is malformed")
    if pages < 1:
        raise InventoryCollectionError("provider returned no inventory pages")
    return Collection(
        resources=resources,
        pages=pages,
        response_sha256=hashlib.sha256(_canonical(page_bodies)).hexdigest(),
    )


def _canonical_resource_id(item: dict, *, fallback: str | None = None) -> str:
    value = item.get("selfLink") or item.get("name") or fallback
    if not isinstance(value, str) or not value:
        raise InventoryCollectionError("provider resource has no stable canonical ID")
    return value


class GCPInventoryAdapter:
    """Collect all current workload/derived-resource audit surfaces.

    The caller supplies a token with read-only permissions and a transport.
    The token is never persisted or included in an exception. This class has
    no default-credential discovery and exposes no non-GET operation.
    """

    def __init__(self, transport: ReadOnlyTransport, *, access_token: str):
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("an explicit short-lived access token is required")
        self._transport = transport
        self._token = access_token

    def _list(
        self, url: str, *, params: dict | None = None, aggregated: bool = False,
        aggregated_key: str | None = None,
        collection_key: str = "items",
    ) -> Collection:
        return _response_pages(
            self._transport,
            url,
            token=self._token,
            params=params,
            collection_key=collection_key,
            aggregated=aggregated,
            aggregated_resource_key=aggregated_key,
        )

    def collect(
        self, manifest: dict, *, observed_at: str, expected_auditor_email: str,
    ) -> dict:
        """Return normalized, exact-project evidence or fail without a report."""
        manifest_sha256 = manifest_fingerprint(manifest)
        run_id = manifest.get("run_id")
        project_id = manifest.get("scope", {}).get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise InventoryCollectionError("manifest has no exact GCP project ID")
        if (not isinstance(expected_auditor_email, str)
                or PRINCIPAL_EMAIL.fullmatch(expected_auditor_email) is None):
            raise InventoryCollectionError("expected auditor email is malformed")
        try:
            verified_user = verify_access_token_principal(
                self._transport, self._token,
                expected_email=expected_auditor_email,
            )
        except IdentityVerificationError as exc:
            raise InventoryCollectionError(str(exc)) from None
        if not isinstance(observed_at, str):
            raise InventoryCollectionError(
                "observation time must be an ISO-8601 timestamp"
            )
        try:
            observation = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            raise InventoryCollectionError(
                "observation time must be an ISO-8601 timestamp"
            ) from None
        if observation.tzinfo is None or observation.utcoffset() is None:
            raise InventoryCollectionError("observation time must include a timezone")
        p = quote(project_id, safe="")
        compute = f"https://compute.googleapis.com/compute/v1/projects/{p}"
        container = f"https://container.googleapis.com/v1/projects/{p}"
        hub = f"https://gkehub.googleapis.com/v1/projects/{p}"
        pubsub = f"https://pubsub.googleapis.com/v1/projects/{p}"
        storage = "https://storage.googleapis.com/storage/v1"
        collections: dict[str, Collection] = {}

        def add(
            surface: str, url: str, *, params=None, aggregated=False,
            aggregated_key=None, collection_key="items",
        ):
            collections[surface] = self._list(
                url, params=params, aggregated=aggregated,
                aggregated_key=aggregated_key,
                collection_key=collection_key,
            )

        add(
            "compute.addresses", f"{compute}/aggregated/addresses",
            aggregated=True, aggregated_key="addresses",
        )
        add(
            "compute.backend_services", f"{compute}/aggregated/backendServices",
            aggregated=True, aggregated_key="backendServices",
        )
        add(
            "compute.disks", f"{compute}/aggregated/disks",
            aggregated=True, aggregated_key="disks",
        )
        add("compute.firewalls", f"{compute}/global/firewalls")
        add(
            "compute.forwarding_rules", f"{compute}/aggregated/forwardingRules",
            aggregated=True, aggregated_key="forwardingRules",
        )
        add(
            "compute.health_checks", f"{compute}/aggregated/healthChecks",
            aggregated=True, aggregated_key="healthChecks",
        )
        add(
            "compute.instances", f"{compute}/aggregated/instances",
            aggregated=True, aggregated_key="instances",
        )
        add(
            "compute.instance_groups", f"{compute}/aggregated/instanceGroups",
            aggregated=True, aggregated_key="instanceGroups",
        )
        add("compute.networks", f"{compute}/global/networks")
        add("compute.routes", f"{compute}/global/routes")
        router_collection = self._list(
            f"{compute}/aggregated/routers", aggregated=True,
            aggregated_key="routers",
        )
        collections["compute.routers"] = router_collection
        add(
            "compute.subnetworks", f"{compute}/aggregated/subnetworks",
            aggregated=True, aggregated_key="subnetworks",
        )
        add(
            "compute.target_instances", f"{compute}/aggregated/targetInstances",
            aggregated=True, aggregated_key="targetInstances",
        )
        add(
            "compute.target_pools", f"{compute}/aggregated/targetPools",
            aggregated=True, aggregated_key="targetPools",
        )

        cluster_collection = self._list(
            f"{container}/locations/-/clusters", collection_key="clusters"
        )
        clusters = cluster_collection.resources
        cluster_ids = []
        node_pools = []
        node_pool_responses = []
        for cluster in clusters:
            cluster_name = cluster.get("name")
            location = (
                cluster.get("location")
                or cluster.get("zone")
                or cluster.get("region")
            )
            if not isinstance(cluster_name, str) or not isinstance(location, str):
                raise InventoryCollectionError(
                    "GKE cluster lacks a stable parent identity"
                )
            parent = (
                f"{container}/locations/{quote(location, safe='')}/clusters/"
                f"{quote(cluster_name, safe='')}"
            )
            cluster_ids.append({
                "id": (
                    f"projects/{project_id}/locations/{location}/clusters/"
                    f"{cluster_name}"
                )
            })
            result = self._list(f"{parent}/nodePools", collection_key="nodePools")
            for node_pool in result.resources:
                node_pool_name = node_pool.get("name")
                if not isinstance(node_pool_name, str) or not node_pool_name:
                    raise InventoryCollectionError("GKE node pool lacks a stable name")
                node_pools.append({
                    "id": f"projects/{project_id}/locations/{location}/clusters/"
                    f"{cluster_name}/nodePools/{node_pool_name}"
                })
            node_pool_responses.append((parent, result.response_sha256, result.pages))
        collections["container.clusters"] = self._synthetic_collection(
            cluster_ids,
            source=(cluster_collection.response_sha256, cluster_collection.pages),
            pages=cluster_collection.pages,
        )
        collections["container.node_pools"] = self._synthetic_collection(
            node_pools, source=node_pool_responses,
            pages=sum(item[2] for item in node_pool_responses),
        )

        membership_collection = self._list(
            f"{hub}/locations/-/memberships", collection_key="resources"
        )
        collections["gkehub.memberships"] = membership_collection
        location_collection = self._list(
            f"https://gkehub.googleapis.com/v1beta/projects/{p}/locations",
            collection_key="locations",
        )
        features = []
        feature_responses = []
        location_ids = {"global"}
        for location in location_collection.resources:
            location_id = location.get("locationId")
            if not isinstance(location_id, str) or not location_id:
                raise InventoryCollectionError("GKE Hub location lacks a stable ID")
            location_ids.add(location_id)
        for location_id in sorted(location_ids):
            feature_result = self._list(
                f"{hub}/locations/{quote(location_id, safe='')}/features",
                collection_key="resources",
            )
            features.extend(feature_result.resources)
            feature_responses.append(
                (location_id, feature_result.response_sha256, feature_result.pages)
            )
        collections["gkehub.features"] = self._synthetic_collection(
            features,
            source=(location_collection.response_sha256, feature_responses),
            pages=(
                location_collection.pages
                + sum(item[2] for item in feature_responses)
            ),
        )
        feature_memberships = []
        feature_responses = []
        for membership in membership_collection.resources:
            name = membership.get("name")
            if not isinstance(name, str) or not name:
                raise InventoryCollectionError("GKE Hub Membership lacks a stable name")
            result = self._list(
                f"https://gkehub.googleapis.com/v2/{name}/features",
                collection_key="membershipFeatures",
            )
            feature_memberships.extend(result.resources)
            feature_responses.append((name, result.response_sha256, result.pages))
        collections["gkehub.feature_memberships"] = self._synthetic_collection(
            feature_memberships, source=feature_responses,
            pages=sum(item[2] for item in feature_responses),
        )

        add(
            "pubsub.subscriptions", f"{pubsub}/subscriptions",
            collection_key="subscriptions",
        )
        add("pubsub.topics", f"{pubsub}/topics", collection_key="topics")
        bucket_collection = self._list(f"{storage}/b", params={"project": project_id})
        buckets = bucket_collection.resources
        collections["storage.buckets"] = bucket_collection
        objects = []
        versions = []
        object_responses = []
        for bucket in buckets:
            bucket_name = bucket.get("name")
            if not isinstance(bucket_name, str) or not bucket_name:
                raise InventoryCollectionError("Storage bucket lacks a stable name")
            result = self._list(
                f"{storage}/b/{quote(bucket_name, safe='')}/o",
                params={"versions": "true"},
            )
            object_responses.append((bucket_name, result.response_sha256, result.pages))
            for item in result.resources:
                object_name = item.get("name")
                generation = item.get("generation")
                if not isinstance(object_name, str) or not isinstance(generation, str):
                    raise InventoryCollectionError(
                        "Storage object lacks name or generation"
                    )
                encoded_object_name = quote(object_name, safe="/-_.~")
                versions.append({
                    "id": f"gs://{bucket_name}/{encoded_object_name}@{generation}",
                })
                if item.get("timeDeleted") is None:
                    objects.append({"id": f"gs://{bucket_name}/{encoded_object_name}"})
        collections["storage.objects"] = self._synthetic_collection(
            objects, source=object_responses,
            pages=sum(item[2] for item in object_responses),
        )
        collections["storage.object_versions"] = self._synthetic_collection(
            versions, source=object_responses,
            pages=sum(item[2] for item in object_responses),
        )

        # NAT configurations are nested in the exact router responses.
        nat_source = router_collection
        nat_resources = []
        for router in nat_source.resources:
            for nat in router.get("nats", []):
                router_id = _canonical_resource_id(router)
                nat_name = nat.get("name") if isinstance(nat, dict) else None
                if not isinstance(nat_name, str) or not nat_name:
                    raise InventoryCollectionError(
                        "Cloud NAT configuration lacks a stable name"
                    )
                nat_resources.append({"id": f"{router_id}/nats/{nat_name}"})
        collections["compute.router_nats"] = self._synthetic_collection(
            nat_resources,
            source=(nat_source.response_sha256, nat_source.pages),
            pages=nat_source.pages,
        )

        if set(collections) != WORKLOAD_SURFACES:
            raise InventoryCollectionError(
                "adapter surface set does not match audit contract"
            )
        normalized = []
        coverage = {}
        for surface in sorted(WORKLOAD_SURFACES):
            collection = collections[surface]
            coverage[surface] = {
                "complete": True,
                "pages": collection.pages,
                "resource_count": len(collection.resources),
                "response_sha256": collection.response_sha256,
            }
            for item in collection.resources:
                resource_id = item.get("id") or _canonical_resource_id(item)
                normalized.append({
                    "surface": surface,
                    "project_id": project_id,
                    "id": resource_id,
                })
        return {
            "schema": AUDIT_SCHEMA,
            "run_id": run_id,
            "project_id": project_id,
            "manifest_sha256": manifest_sha256,
            "observed_at": observed_at,
            "auditor_id": verified_user.auditor_id,
            "auditor_email": verified_user.email,
            "coverage": coverage,
            "resources": normalized,
        }

    @staticmethod
    def _synthetic_collection(
        resources: list[dict], *, source: object, pages: int = 1,
    ) -> Collection:
        return Collection(
            resources=resources,
            pages=max(1, pages),
            response_sha256=hashlib.sha256(
                _canonical({"source": source, "resources": resources})
            ).hexdigest(),
        )
