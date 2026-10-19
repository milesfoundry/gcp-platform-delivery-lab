"""Credential-free tests for the read-only GCP REST inventory adapter."""

from __future__ import annotations

from copy import deepcopy
import unittest

from gcp_inventory_adapter import (
    GCPInventoryAdapter,
    InventoryCollectionError,
    _response_pages,
)
from independent_auditor import WORKLOAD_SURFACES, assess_workload_inventory
from test_run_contract import PROJECT, intent


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


class FakeTransport:
    def __init__(self, handler=None):
        self.handler = handler or self.empty_inventory
        self.calls = []

    @staticmethod
    def empty_inventory(url, params):
        if url == "https://openidconnect.googleapis.com/v1/userinfo":
            return {
                "sub": "12345678901234567890",
                "email": "independent-reader@example.com",
                "email_verified": True,
            }
        if "/aggregated/" in url:
            return {"items": {}}
        if url.endswith("/clusters"):
            return {"clusters": []}
        if url.endswith("/nodePools"):
            return {"nodePools": []}
        if url.endswith("/locations"):
            return {"locations": []}
        if "/memberships/" in url and url.endswith("/features"):
            return {"membershipFeatures": []}
        if url.endswith("/features"):
            return {"resources": []}
        if url.endswith("/memberships"):
            return {"resources": []}
        if url.endswith("/topics"):
            return {"topics": []}
        if url.endswith("/subscriptions"):
            return {"subscriptions": []}
        return {"items": []}

    def get(self, url, *, params, headers, timeout, allow_redirects):
        self.calls.append(
            (url, deepcopy(params), deepcopy(headers), timeout, allow_redirects)
        )
        result = self.handler(url, params)
        if isinstance(result, FakeResponse):
            return result
        return FakeResponse(result)


class GCPInventoryAdapterTests(unittest.TestCase):
    def test_complete_empty_snapshot_covers_exact_auditor_surface_set(self):
        manifest = intent()
        transport = FakeTransport()
        report = GCPInventoryAdapter(
            transport, access_token="short-lived-test-token"
        ).collect(
            manifest,
            observed_at="2026-10-19T02:00:00Z",
            expected_auditor_email="independent-reader@example.com",
        )

        self.assertEqual(set(report["coverage"]), WORKLOAD_SURFACES)
        self.assertEqual(report["resources"], [])
        self.assertTrue(all(entry["complete"] for entry in report["coverage"].values()))
        self.assertTrue(
            all(entry["pages"] >= 1 for entry in report["coverage"].values())
        )
        self.assertEqual(assess_workload_inventory(manifest, report).result, "passed")
        api_calls = [
            call for call in transport.calls
            if call[0] != "https://openidconnect.googleapis.com/v1/userinfo"
        ]
        self.assertTrue(all(call[2]["Authorization"] == "Bearer short-lived-test-token"
                            for call in api_calls))
        identity_call = next(
            call for call in transport.calls
            if call[0] == "https://openidconnect.googleapis.com/v1/userinfo"
        )
        self.assertEqual(identity_call[1], {})
        self.assertEqual(identity_call[2]["Authorization"], "Bearer short-lived-test-token")
        self.assertTrue(all(call[3] == 30 for call in transport.calls))
        self.assertTrue(all(call[4] is False for call in transport.calls))

    def test_pagination_collects_every_page_and_fingerprints_responses(self):
        transport = FakeTransport(
            lambda _url, params: (
                {"items": [{"name": "first"}], "nextPageToken": "next"}
                if "pageToken" not in params
                else {"items": [{"name": "second"}]}
            )
        )
        collected = _response_pages(
            transport,
            "https://compute.googleapis.com/compute/v1/projects/p/global/networks",
            token="token",
        )
        self.assertEqual(collected.pages, 2)
        self.assertEqual(
            [item["name"] for item in collected.resources], ["first", "second"]
        )
        self.assertEqual(transport.calls[1][1]["pageToken"], "next")
        self.assertEqual(len(collected.response_sha256), 64)

    def test_bearer_token_cannot_be_sent_to_non_google_or_http_urls(self):
        transport = FakeTransport()
        for url in (
            "http://compute.googleapis.com/compute/v1/projects/p/global/networks",
            "https://example.test/collect",
        ):
            with self.subTest(url=url), self.assertRaises(InventoryCollectionError):
                _response_pages(transport, url, token="must-not-leak")
        self.assertEqual(transport.calls, [])

    def test_refuses_token_for_a_different_auditor_before_inventory_collection(self):
        transport = FakeTransport()
        with self.assertRaises(InventoryCollectionError):
            GCPInventoryAdapter(
                transport, access_token="short-lived-test-token"
            ).collect(
                intent(),
                observed_at="2026-10-19T02:00:00Z",
                expected_auditor_email="different-person@example.com",
            )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(
            transport.calls[0][0], "https://openidconnect.googleapis.com/v1/userinfo"
        )

    def test_provider_collection_keys_and_derived_resources_are_normalized(self):
        manifest = intent()

        def populated(url, _params):
            if url == "https://openidconnect.googleapis.com/v1/userinfo":
                return {
                    "sub": "12345678901234567890",
                    "email": "independent-reader@example.com",
                    "email_verified": True,
                }
            if "/aggregated/routers" in url:
                return {"items": {"regions/europe-west2": {"routers": [{
                    "selfLink": (
                        "https://compute.googleapis.com/compute/v1/projects/"
                        f"{PROJECT}/regions/europe-west2/routers/router-a"
                    ),
                    "nats": [{"name": "nat-a"}],
                }]}}}
            if "/aggregated/" in url:
                key = url.rsplit("/", 1)[-1]
                return {"items": {"zones/europe-west2-a": {key: [{
                    "selfLink": (
                        "https://compute.googleapis.com/compute/v1/projects/"
                        f"{PROJECT}/zones/europe-west2-a/{key}/asset-a"
                    ),
                }]}}}
            if url.endswith("/clusters"):
                return {"clusters": [{"name": "cluster-a", "location": "europe-west2"}]}
            if url.endswith("/nodePools"):
                return {"nodePools": [{"name": "pool-a"}]}
            if url.endswith("/locations"):
                return {"locations": [{"locationId": "global"}]}
            if "/memberships/" in url and url.endswith("/features"):
                return {"membershipFeatures": [{
                    "name": (
                        f"projects/{PROJECT}/locations/global/memberships/"
                        "member-a/features/configmanagement"
                    )
                }]}
            if url.endswith("/features"):
                return {"resources": [{
                    "name": (
                        f"projects/{PROJECT}/locations/global/features/"
                        "configmanagement"
                    )
                }]}
            if url.endswith("/memberships"):
                return {"resources": [{
                    "name": f"projects/{PROJECT}/locations/global/memberships/member-a"
                }]}
            if url.endswith("/topics"):
                return {"topics": [{"name": f"projects/{PROJECT}/topics/topic-a"}]}
            if url.endswith("/subscriptions"):
                return {"subscriptions": [{
                    "name": f"projects/{PROJECT}/subscriptions/sub-a"
                }]}
            if url.endswith("/storage/v1/b"):
                return {"items": [{
                    "name": "bucket-a",
                    "softDeletePolicy": {"retentionDurationSeconds": "604800"},
                }]}
            if url.endswith("/b/bucket-a/o") and _params.get("softDeleted") == "true":
                return {"items": [{
                    "name": "active/report.json",
                    "generation": "5",
                    "softDeleteTime": "2026-10-19T00:00:00Z",
                    "hardDeleteTime": "2026-10-26T00:00:00Z",
                }]}
            if url.endswith("/b/bucket-a/o"):
                return {"items": [
                {"name": "active/report.json", "generation": "7"},
                {"name": "active/report?special.json", "generation": "8"},
                {
                        "name": "active/report.json",
                        "generation": "6",
                        "timeDeleted": "2026-10-01T00:00:00Z",
                    },
                ]}
            return {"items": [{
                "selfLink": (
                    "https://compute.googleapis.com/compute/v1/projects/"
                    f"{PROJECT}/global/{url.rsplit('/', 1)[-1]}/asset-a"
                ),
            }]}

        transport = FakeTransport(populated)
        report = GCPInventoryAdapter(
            transport, access_token="short-lived-test-token"
        ).collect(
            manifest, observed_at="2026-10-19T02:00:00Z", expected_auditor_email="independent-reader@example.com"
        )
        resources = {(item["surface"], item["id"]) for item in report["resources"]}
        self.assertTrue(any(
            url.endswith("/b/bucket-a/o") and params.get("softDeleted") == "true"
            for url, params, *_ in transport.calls
        ))
        self.assertIn(
            ("pubsub.topics", f"projects/{PROJECT}/topics/topic-a"), resources
        )
        self.assertIn(
            ("pubsub.subscriptions", f"projects/{PROJECT}/subscriptions/sub-a"),
            resources,
        )
        self.assertIn(
            (
                "container.clusters",
                f"projects/{PROJECT}/locations/europe-west2/clusters/cluster-a",
            ),
            resources,
        )
        self.assertIn(
            (
                "container.node_pools",
                f"projects/{PROJECT}/locations/europe-west2/clusters/cluster-a/"
                "nodePools/pool-a",
            ),
            resources,
        )
        nat_id = (
            "https://compute.googleapis.com/compute/v1/projects/"
            f"{PROJECT}/regions/europe-west2/routers/router-a/nats/nat-a"
        )
        self.assertIn(("compute.router_nats", nat_id), resources)
        self.assertIn(
            (
                "storage.objects",
                f"projects/{PROJECT}/buckets/bucket-a/objects/active%2Freport.json@live",
            ),
            resources,
        )
        self.assertIn(
            (
                "storage.objects",
                f"projects/{PROJECT}/buckets/bucket-a/objects/active%2Freport%3Fspecial.json@live",
            ),
            resources,
        )
        self.assertIn(
            (
                "storage.object_versions",
                f"projects/{PROJECT}/buckets/bucket-a/objects/active%2Freport.json@6",
            ),
            resources,
        )
        self.assertIn(
            (
                "storage.object_versions",
                f"projects/{PROJECT}/buckets/bucket-a/objects/active%2Freport.json@5",
            ),
            resources,
        )
        self.assertIn(
            (
                "storage.object_versions",
                f"projects/{PROJECT}/buckets/bucket-a/objects/active%2Freport%3Fspecial.json@8",
            ),
            resources,
        )
        assessment = assess_workload_inventory(manifest, report)
        self.assertEqual(assessment.result, "failed")
        self.assertTrue(assessment.complete)

    def test_aggregated_pages_use_resource_key_and_reject_unreachable_zones(self):
        transport = FakeTransport(
            lambda _url, _params: {
                "items": {
                    "zones/europe-west2-a": {
                        "addresses": [{"name": "address-a"}],
                    },
                    "zones/europe-west2-b": {
                        "addresses": [],
                    },
                }
            }
        )
        collected = _response_pages(
            transport,
            "https://compute.googleapis.com/compute/v1/projects/p/aggregated/addresses",
            token="token",
            aggregated=True,
            aggregated_resource_key="addresses",
        )
        self.assertEqual([item["name"] for item in collected.resources], ["address-a"])

        no_results = FakeTransport(
            lambda _url, _params: {
                "items": {
                    "zones/europe-west2-b": {
                        "warning": {"code": "NO_RESULTS_ON_PAGE"},
                    }
                }
            }
        )
        collected = _response_pages(
            no_results,
            "https://compute.googleapis.com/compute/v1/projects/p/aggregated/addresses",
            token="token",
            aggregated=True,
            aggregated_resource_key="addresses",
        )
        self.assertEqual(collected.resources, [])

        transport = FakeTransport(
            lambda _url, _params: {
                    "items": {
                        "zones/europe-west2-c": {
                            "warning": {"code": "UNREACHABLE"}
                        }
                    }
            }
        )
        with self.assertRaises(InventoryCollectionError):
            _response_pages(
                transport,
                (
                    "https://compute.googleapis.com/compute/v1/projects/"
                    "p/aggregated/addresses"
                ),
                token="token",
                aggregated=True,
                aggregated_resource_key="addresses",
            )

    def test_any_api_failure_aborts_collection_without_partial_report(self):
        def fail_one(url, _params):
            if "/global/networks" in url:
                return FakeResponse({"error": "permission denied"}, status_code=403)
            return FakeTransport.empty_inventory(url, _params)

        adapter = GCPInventoryAdapter(FakeTransport(fail_one), access_token="token")
        with self.assertRaisesRegex(InventoryCollectionError, "HTTP 403"):
            adapter.collect(
                intent(), observed_at="2026-10-19T02:00:00Z", expected_auditor_email="independent-reader@example.com"
            )

    def test_adapter_requires_an_explicit_token_and_exact_project(self):
        with self.assertRaises(ValueError):
            GCPInventoryAdapter(FakeTransport(), access_token="")
        malformed = intent()
        malformed["scope"]["project_id"] = None
        adapter = GCPInventoryAdapter(FakeTransport(), access_token="token")
        with self.assertRaises(ValueError):
            adapter.collect(
                malformed, observed_at="2026-10-19T02:00:00Z", expected_auditor_email="independent-reader@example.com"
            )


if __name__ == "__main__":
    unittest.main()
