"""Mocked GCP project, billing, and Service Usage lifecycle contracts."""

from __future__ import annotations

import json
import unittest

from gcp_project_lifecycle_adapter import (
    BILLING_TYPE,
    PROJECT_TYPE,
    SERVICE_TYPE,
    GCPProjectLifecycleAdapter,
    ProjectLifecycleError,
)


PROJECT = "sample-lab-123"
RUN = "lab-20261018-a1b2"
PARENT = "folders/123456"
BILLING = "billingAccounts/ABCDEF-123456-GHIJKL"
API = "compute.googleapis.com"
TOKEN = "ephemeral-test-token"


class FakeTokenSource:
    def access_token(self):
        return TOKEN


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = {} if body is None else body

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


def adapter(transport, *, services=None):
    return GCPProjectLifecycleAdapter(
        transport,
        FakeTokenSource(),
        project_id=PROJECT,
        run_id=RUN,
        parent=PARENT,
        billing_account_name=BILLING,
        enabled_services=sorted(services or [API]),
    )


def expected_project():
    return {
        "projectId": PROJECT,
        "displayName": RUN,
        "parent": PARENT,
        "labels": {"lab_run_id": RUN},
    }


class GCPProjectLifecycleAdapterTests(unittest.TestCase):
    def test_create_project_is_scoped_and_records_async_operation(self):
        transport = FakeTransport([
            FakeResponse(200, {"name": "operations/pc.123", "done": False})
        ])
        result = adapter(transport).submit(
            "create", PROJECT_TYPE, f"projects/{PROJECT}", {
                "project_id": PROJECT,
                "display_name": RUN,
                "parent": PARENT,
                "labels": {"lab_run_id": RUN},
            },
        )
        self.assertEqual(result.state, "running")
        self.assertEqual(
            result.operation_name,
            f"resourcemanager/projects/{PROJECT}/operations/pc.123",
        )
        method, url, headers, body, _, redirects = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://cloudresourcemanager.googleapis.com/v3/projects")
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(json.loads(body), expected_project())
        self.assertFalse(redirects)

    def test_project_create_rejects_foreign_ids_and_unreviewed_parent(self):
        transport = FakeTransport([])
        provider = adapter(transport)
        request = {
            "project_id": PROJECT,
            "display_name": RUN,
            "parent": PARENT,
            "labels": {"lab_run_id": RUN},
        }
        with self.assertRaisesRegex(ProjectLifecycleError, "does not match this run"):
            provider.submit("create", PROJECT_TYPE, "projects/other-lab-456", request)
        request["parent"] = "organizations/987654"
        with self.assertRaisesRegex(ProjectLifecycleError, "differs from the reviewed run"):
            provider.submit("create", PROJECT_TYPE, f"projects/{PROJECT}", request)
        self.assertEqual(transport.calls, [])

    def test_create_operation_is_polled_only_through_run_scoped_name(self):
        project = {
            "projectId": PROJECT,
            "displayName": RUN,
            "parent": PARENT,
            "state": "ACTIVE",
            "labels": {"lab_run_id": RUN},
        }
        transport = FakeTransport([
            FakeResponse(200, {
                "name": "operations/pc.123",
                "done": True,
                "response": project,
            })
        ])
        operation_name = f"resourcemanager/projects/{PROJECT}/operations/pc.123"
        result = adapter(transport).poll_operation(operation_name)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(
            transport.calls[0][1],
            "https://cloudresourcemanager.googleapis.com/v3/operations/pc.123",
        )

        foreign_transport = FakeTransport([])
        with self.assertRaisesRegex(ProjectLifecycleError, "outside this run"):
            adapter(foreign_transport).poll_operation(
                "resourcemanager/projects/other-lab-456/operations/pc.123"
            )
        self.assertEqual(foreign_transport.calls, [])

    def test_created_project_reconciliation_checks_parent_and_run_label(self):
        transport = FakeTransport([
            FakeResponse(200, {
                "projectId": PROJECT,
                "displayName": RUN,
                "parent": PARENT,
                "state": "ACTIVE",
                "labels": {"lab_run_id": RUN},
            }),
            FakeResponse(200, {
                "projectId": PROJECT,
                "displayName": RUN,
                "parent": "folders/456",
                "labels": {"lab_run_id": RUN},
            }),
        ])
        provider = adapter(transport)
        self.assertEqual(provider.inspect_resource(PROJECT_TYPE, f"projects/{PROJECT}").state,
                         "present")
        self.assertEqual(provider.inspect_resource(PROJECT_TYPE, f"projects/{PROJECT}").state,
                         "unknown")

    def test_billing_link_and_unlink_use_only_reviewed_account(self):
        transport = FakeTransport([
            FakeResponse(200, {
                "name": f"projects/{PROJECT}",
                "billingAccountName": BILLING,
                "billingEnabled": True,
            }),
            FakeResponse(200, {
                "name": f"projects/{PROJECT}",
                "billingAccountName": "",
                "billingEnabled": False,
            }),
        ])
        provider = adapter(transport)
        target = f"projects/{PROJECT}"
        linked = provider.submit(
            "create", BILLING_TYPE, target,
            {"billing_account_name": BILLING},
        )
        unlinked = provider.submit(
            "delete", BILLING_TYPE, target,
            {"billing_account_name": ""},
        )
        self.assertEqual((linked.state, unlinked.state), ("succeeded", "succeeded"))
        self.assertEqual([call[0] for call in transport.calls], ["PUT", "PUT"])
        self.assertEqual(
            transport.calls[0][1],
            f"https://cloudbilling.googleapis.com/v1/projects/{PROJECT}/billingInfo",
        )
        self.assertEqual(
            json.loads(transport.calls[0][3]), {"billingAccountName": BILLING}
        )
        self.assertEqual(
            json.loads(transport.calls[1][3]), {"billingAccountName": ""}
        )

        with self.assertRaisesRegex(ProjectLifecycleError, "reviewed account"):
            adapter(FakeTransport([])).submit(
                "create", BILLING_TYPE, target,
                {"billing_account_name": "billingAccounts/OTHER"},
            )

    def test_service_enablement_is_limited_to_manifest_allowlist(self):
        transport = FakeTransport([
            FakeResponse(200, {"name": "operations/enable.456", "done": False})
        ])
        provider = adapter(transport, services=[API])
        result = provider.submit(
            "create", SERVICE_TYPE,
            f"projects/{PROJECT}/services/{API}", {},
        )
        self.assertEqual(result.state, "running")
        self.assertEqual(
            result.operation_name,
            f"serviceusage/projects/{PROJECT}/services/{API}/operations/enable.456",
        )
        self.assertEqual(
            transport.calls[0][1],
            f"https://serviceusage.googleapis.com/v1/projects/{PROJECT}/services/{API}:enable",
        )
        denied = FakeTransport([])
        with self.assertRaisesRegex(ProjectLifecycleError, "allowlist"):
            adapter(denied, services=[API]).submit(
                "create", SERVICE_TYPE,
                f"projects/{PROJECT}/services/storage.googleapis.com", {},
            )
        self.assertEqual(denied.calls, [])

    def test_service_operation_poll_is_bound_to_exact_service(self):
        transport = FakeTransport([
            FakeResponse(200, {"name": "operations/enable.456", "done": False})
        ])
        operation = (
            f"serviceusage/projects/{PROJECT}/services/{API}/operations/enable.456"
        )
        result = adapter(transport).poll_operation(operation)
        self.assertEqual(result.state, "running")
        self.assertEqual(
            transport.calls[0][1],
            "https://serviceusage.googleapis.com/v1/operations/enable.456",
        )
        denied = FakeTransport([])
        with self.assertRaisesRegex(ProjectLifecycleError, "service allowlist"):
            adapter(denied).poll_operation(
                f"serviceusage/projects/{PROJECT}/services/storage.googleapis.com/operations/enable.456"
            )
        self.assertEqual(denied.calls, [])

    def test_project_deletion_remains_outside_adapter_authority(self):
        transport = FakeTransport([])
        with self.assertRaisesRegex(ProjectLifecycleError, "separate reviewed adapter"):
            adapter(transport).submit(
                "delete", PROJECT_TYPE, f"projects/{PROJECT}", {}
            )
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
