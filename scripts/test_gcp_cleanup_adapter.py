"""Credential-free contracts for the GCP delete-only provider adapter."""

from __future__ import annotations

import json
import unittest

from gcp_cleanup_adapter import (
    CleanupAdapterError,
    GCPCleanupAdapter,
)


PROJECT = "sample-lab-123"
RUN = "lab-20261018-a1b2"
TOKEN = "ephemeral-test-token"


class FakeTokenSource:
    def access_token(self):
        return TOKEN


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout, allow_redirects):
        self.calls.append((method, url, headers, body, timeout, allow_redirects))
        if not self.responses:
            raise AssertionError("unexpected provider request")
        return self.responses.pop(0)


def adapter(transport, *, cluster_delete_authorizer=None):
    return GCPCleanupAdapter(
        transport,
        FakeTokenSource(),
        project_id=PROJECT,
        run_id=RUN,
        cluster_delete_authorizer=cluster_delete_authorizer,
    )


class GCPCleanupAdapterTests(unittest.TestCase):
    def test_delete_network_uses_exact_compute_path_and_stable_request_id(self):
        resource_id = (
            f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/"
            f"global/networks/{RUN}"
        )
        transport = FakeTransport([
            FakeResponse(200, {"name": "op-1", "status": "RUNNING", "selfLink":
                f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/"
                "global/operations/op-1"})
        ])
        result = adapter(transport).submit("delete", "gcp.compute.network", resource_id, {})
        self.assertEqual(result.state, "running")
        self.assertEqual(
            result.operation_name,
            f"compute/projects/{PROJECT}/global/operations/op-1",
        )
        call = transport.calls[0]
        self.assertEqual(call[0], "DELETE")
        self.assertTrue(call[1].startswith(
            f"https://compute.googleapis.com/compute/v1/projects/{PROJECT}/global/networks/{RUN}?requestId="
        ))
        self.assertEqual(call[2]["Authorization"], f"Bearer {TOKEN}")
        self.assertFalse(call[5])

    def test_compute_operation_poll_is_bound_to_project(self):
        transport = FakeTransport([FakeResponse(200, {"name": "op-1", "status": "DONE"})])
        result = adapter(transport).poll_operation(
            f"compute/projects/{PROJECT}/global/operations/op-1"
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(
            transport.calls[0][1],
            f"https://compute.googleapis.com/compute/v1/projects/{PROJECT}/global/operations/op-1",
        )

        foreign = FakeTransport([])
        with self.assertRaisesRegex(CleanupAdapterError, "outside the run project"):
            adapter(foreign).poll_operation(
                "compute/projects/other-lab-456/global/operations/op-1"
            )
        self.assertEqual(foreign.calls, [])

    def test_gke_node_pool_delete_targets_only_declared_pool(self):
        resource_id = (
            f"projects/{PROJECT}/locations/europe-west2/clusters/{RUN}/"
            "nodePools/lab"
        )
        transport = FakeTransport([
            FakeResponse(200, {
                "name": f"projects/{PROJECT}/locations/europe-west2/operations/op-2",
                "status": "RUNNING",
                "selfLink": (
                    f"https://container.googleapis.com/v1/projects/{PROJECT}/"
                    "locations/europe-west2/operations/op-2"
                ),
            })
        ])
        result = adapter(transport).submit("delete", "gcp.gke.node_pool", resource_id, {})
        self.assertEqual(result.state, "running")
        self.assertEqual(
            transport.calls[0][1],
            f"https://container.googleapis.com/v1/{resource_id}",
        )
        self.assertEqual(
            result.operation_name,
            f"container/projects/{PROJECT}/locations/europe-west2/operations/op-2",
        )

    def test_gke_operation_poll_rejects_wrong_project_before_transport(self):
        transport = FakeTransport([])
        with self.assertRaisesRegex(CleanupAdapterError, "outside the run project"):
            adapter(transport).poll_operation(
                "container/projects/other-lab-456/locations/europe-west2/operations/op-2"
            )
        self.assertEqual(transport.calls, [])

    def test_cluster_delete_requires_exact_protection_review(self):
        resource_id = (
            f"projects/{PROJECT}/locations/europe-west2/clusters/{RUN}"
        )
        transport = FakeTransport([])
        with self.assertRaisesRegex(CleanupAdapterError, "requires a separate protection review"):
            adapter(transport).submit("delete", "gcp.gke.cluster", resource_id, {})
        self.assertEqual(transport.calls, [])

        approved = []
        reviewed = adapter(
            FakeTransport([FakeResponse(200, {
                "name": "op-cluster",
                "status": "RUNNING",
                "selfLink": (
                    f"https://container.googleapis.com/v1/projects/{PROJECT}/"
                    "locations/europe-west2/operations/op-cluster"
                ),
            })]),
            cluster_delete_authorizer=lambda exact_id: approved.append(exact_id) or True,
        )
        result = reviewed.submit("delete", "gcp.gke.cluster", resource_id, {})
        self.assertEqual(result.state, "running")
        self.assertEqual(approved, [resource_id])

    def test_router_nat_cleanup_uses_fingerprint_and_preserves_other_nat(self):
        router_path = f"/compute/v1/projects/{PROJECT}/regions/europe-west2/routers/{RUN}"
        resource_id = f"https://www.googleapis.com{router_path}/nats/{RUN}"
        body = {
            "fingerprint": "router-version-4",
            "nats": [
                {"name": RUN, "natIpAllocateOption": "AUTO_ONLY"},
                {"name": "keep-this-nat", "natIpAllocateOption": "AUTO_ONLY"},
            ],
        }
        transport = FakeTransport([
            FakeResponse(200, body),
            FakeResponse(200, {"name": "op-nat", "status": "DONE"}),
        ])
        result = adapter(transport).submit("delete", "gcp.compute.router_nat", resource_id, {})
        self.assertEqual(result.state, "succeeded")
        self.assertEqual([call[0] for call in transport.calls], ["GET", "PATCH"])
        patch_call = transport.calls[1]
        self.assertEqual(patch_call[1], f"https://compute.googleapis.com{router_path}")
        patch_body = json.loads(patch_call[3])
        self.assertEqual(patch_body["fingerprint"], "router-version-4")
        self.assertEqual(patch_body["nats"], [{
            "name": "keep-this-nat", "natIpAllocateOption": "AUTO_ONLY"
        }])

    def test_missing_resource_is_absent_and_unsupported_types_fail_closed(self):
        network_id = f"projects/{PROJECT}/global/networks/{RUN}"
        transport = FakeTransport([FakeResponse(404)])
        result = adapter(transport).inspect_resource("gcp.compute.network", network_id)
        self.assertEqual(result.state, "absent")

        with self.assertRaisesRegex(CleanupAdapterError, "no qualified cleanup mapping"):
            adapter(FakeTransport([])).submit(
                "delete", "gcp.pubsub.topic",
                f"projects/{PROJECT}/topics/{RUN}", {},
            )

    def test_create_and_cross_project_delete_are_rejected_without_requests(self):
        transport = FakeTransport([])
        provider = adapter(transport)
        with self.assertRaisesRegex(CleanupAdapterError, "cannot create"):
            provider.submit("create", "gcp.compute.network", "unused", {})
        with self.assertRaisesRegex(CleanupAdapterError, "outside the exact run project"):
            provider.submit(
                "delete",
                "gcp.compute.network",
                "projects/other-lab-456/global/networks/lab-foreign",
                {},
            )
        self.assertEqual(transport.calls, [])

    def test_resource_ids_reject_query_fragment_and_encoded_path_segments(self):
        transport = FakeTransport([])
        provider = adapter(transport)
        unsafe_ids = [
            f"projects/{PROJECT}/global/networks/{RUN}?alt=json",
            f"projects/{PROJECT}/global/networks/{RUN}%2Fother",
            f"https://user@compute.googleapis.com/compute/v1/projects/{PROJECT}/"
            f"global/networks/{RUN}",
        ]
        for resource_id in unsafe_ids:
            with self.subTest(resource_id=resource_id):
                with self.assertRaises(CleanupAdapterError):
                    provider.submit("delete", "gcp.compute.network", resource_id, {})
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
