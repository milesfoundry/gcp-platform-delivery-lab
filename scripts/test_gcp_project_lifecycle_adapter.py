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
from gcp_token_identity import VerifiedGoogleUser


PROJECT = "sample-lab-123"
RUN = "lab-20261018-a1b2"
PARENT = "folders/123456"
BILLING = "billingAccounts/ABCDEF-123456-GHIJKL"
API = "compute.googleapis.com"
TOKEN = "ephemeral-test-token"
RECLAIMER_EMAIL = "project-reclaimer@example.com"


class FakeTokenSource:
    def access_token(self):
        return TOKEN


class FakeServiceAccountTokenSource:
    def __init__(self, principal_id="google-service-account-12345678901234567890"):
        self.principal = VerifiedGoogleUser(
            auditor_id=principal_id,
            email="janitor@recovery-project.iam.gserviceaccount.com",
        )
        self.verified_tokens = []

    def access_token(self):
        return "cloud-run-access-token"

    def verify_principal(self, token):
        self.verified_tokens.append(token)
        if token != "cloud-run-access-token":
            raise RuntimeError("unexpected token")
        return self.principal


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


class FakeIdentityTransport:
    def __init__(self, *, email=RECLAIMER_EMAIL, subject="12345678901234567890"):
        self.email = email
        self.subject = subject
        self.calls = []

    def get(self, url, *, params, headers, timeout, allow_redirects):
        self.calls.append((url, params, headers, timeout, allow_redirects))
        return FakeResponse(200, {
            "sub": self.subject,
            "email": self.email,
            "email_verified": True,
        })


def adapter(transport, *, services=None, identity_transport=None):
    identity_transport = identity_transport or FakeIdentityTransport()
    return GCPProjectLifecycleAdapter(
        transport,
        FakeTokenSource(),
        project_id=PROJECT,
        run_id=RUN,
        parent=PARENT,
        billing_account_name=BILLING,
        enabled_services=sorted(services or [API]),
        identity_transport=identity_transport,
        project_reclaimer_email=RECLAIMER_EMAIL,
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

    def test_project_delete_waits_for_delete_requested_project_to_be_absent(self):
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
                "billingEnabled": False,
                "billingAccountName": "",
            }),
            FakeResponse(200, {"name": "operations/delete.456", "done": False}),
            FakeResponse(200, {"name": "operations/delete.456", "done": False}),
            FakeResponse(200, {"name": "operations/delete.456", "done": True}),
            FakeResponse(404),
        ])
        provider = adapter(transport)
        target = f"projects/{PROJECT}"
        started = provider.submit("delete", PROJECT_TYPE, target, {})
        self.assertEqual(started.state, "running")
        self.assertEqual(
            started.operation_name,
            f"resourcemanager-delete/projects/{PROJECT}/operations/delete.456",
        )
        self.assertEqual([call[0] for call in transport.calls[:3]], ["GET", "GET", "DELETE"])
        self.assertEqual(
            transport.calls[2][1],
            f"https://cloudresourcemanager.googleapis.com/v3/{target}",
        )
        self.assertIsNone(transport.calls[2][3])

        still_deleting = provider.poll_operation(started.operation_name)
        self.assertEqual(still_deleting.state, "running")
        absent = provider.poll_operation(started.operation_name)
        self.assertEqual(absent.state, "succeeded")
        self.assertEqual(transport.calls[-1][0], "GET")
        self.assertEqual(transport.calls[-1][1],
                         f"https://cloudresourcemanager.googleapis.com/v3/{target}")

    def test_delete_requested_state_is_not_reported_as_project_absent(self):
        transport = FakeTransport([
            FakeResponse(200, {
                "projectId": PROJECT,
                "displayName": RUN,
                "parent": PARENT,
                "state": "DELETE_REQUESTED",
                "labels": {"lab_run_id": RUN},
            }),
        ])
        result = adapter(transport).inspect_resource(PROJECT_TYPE, f"projects/{PROJECT}")
        self.assertEqual(result.state, "retiring")

    def test_reconciles_existing_delete_request_without_submitting_another_delete(self):
        transport = FakeTransport([
            FakeResponse(200, {
                "projectId": PROJECT,
                "displayName": RUN,
                "parent": PARENT,
                "state": "DELETE_REQUESTED",
                "labels": {"lab_run_id": RUN},
            }),
            FakeResponse(404),
        ])
        provider = adapter(transport)
        started = provider.submit("delete", PROJECT_TYPE, f"projects/{PROJECT}", {})
        self.assertEqual(started.state, "running")
        self.assertEqual(started.operation_name,
                         f"resourcemanager-delete/projects/{PROJECT}/operations/accepted")
        result = provider.poll_operation(started.operation_name)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual([call[0] for call in transport.calls], ["GET", "GET"])

    def test_project_delete_refuses_mismatched_project_or_attached_billing(self):
        wrong_project = FakeTransport([
            FakeResponse(200, {
                "projectId": PROJECT,
                "displayName": RUN,
                "parent": "folders/999999",
                "state": "ACTIVE",
                "labels": {"lab_run_id": RUN},
            }),
        ])
        with self.assertRaisesRegex(ProjectLifecycleError, "identity does not match"):
            adapter(wrong_project).submit(
                "delete", PROJECT_TYPE, f"projects/{PROJECT}", {}
            )
        self.assertEqual([call[0] for call in wrong_project.calls], ["GET"])

        attached_billing = FakeTransport([
            FakeResponse(200, {
                "projectId": PROJECT,
                "displayName": RUN,
                "parent": PARENT,
                "state": "ACTIVE",
                "labels": {"lab_run_id": RUN},
            }),
            FakeResponse(200, {
                "projectId": PROJECT,
                "billingEnabled": True,
                "billingAccountName": BILLING,
            }),
        ])
        with self.assertRaisesRegex(ProjectLifecycleError, "detached billing"):
            adapter(attached_billing).submit(
                "delete", PROJECT_TYPE, f"projects/{PROJECT}", {}
            )
        self.assertEqual([call[0] for call in attached_billing.calls], ["GET", "GET"])

    def test_project_retirement_refuses_a_different_verified_google_principal(self):
        identity = FakeIdentityTransport(email="someone-else@example.com")
        transport = FakeTransport([])
        provider = adapter(transport, identity_transport=identity)
        with self.assertRaisesRegex(
            ProjectLifecycleError, "Google principal could not be verified"
        ):
            provider.submit("delete", PROJECT_TYPE, f"projects/{PROJECT}", {})
        self.assertEqual(transport.calls, [])
        self.assertEqual(len(identity.calls), 1)

    def test_named_project_reclaimer_lease_returns_verified_google_subject(self):
        identity = FakeIdentityTransport()
        provider = adapter(FakeTransport([]), identity_transport=identity)
        with self.assertRaisesRegex(ProjectLifecycleError, "named project-reclaimer"):
            provider.authorize_actor(
                "operator:other", "delete", PROJECT_TYPE
            )
        self.assertEqual(identity.calls, [])
        actor = provider.authorize_actor(
            "operator:project-reclaimer", "delete", PROJECT_TYPE
        )
        self.assertEqual(actor.email, RECLAIMER_EMAIL)
        self.assertEqual(actor.auditor_id, "google-user-12345678901234567890")
        self.assertEqual(len(identity.calls), 1)

    def test_per_run_janitor_lease_requires_matching_service_account_principal(self):
        source = FakeServiceAccountTokenSource()
        provider = GCPProjectLifecycleAdapter(
            FakeTransport([]),
            source,
            project_id=PROJECT,
            run_id=RUN,
            parent=PARENT,
            billing_account_name=BILLING,
            enabled_services=[API],
            project_reclaimer_email="janitor@recovery-project.iam.gserviceaccount.com",
            project_reclaimer_auditor_id="google-service-account-12345678901234567890",
        )
        actor = provider.authorize_actor(
            "janitor:google-service-account-12345678901234567890:run-abc-1",
            "delete",
            PROJECT_TYPE,
        )
        self.assertEqual(actor.auditor_id, "google-service-account-12345678901234567890")
        self.assertEqual(source.verified_tokens, ["cloud-run-access-token"])
        with self.assertRaisesRegex(ProjectLifecycleError, "does not match"):
            provider.authorize_actor(
                "janitor:google-service-account-99999999999999999999:run-abc-1",
                "delete",
                PROJECT_TYPE,
            )

        source.principal = VerifiedGoogleUser(
            auditor_id="google-service-account-99999999999999999999",
            email="janitor@recovery-project.iam.gserviceaccount.com",
        )
        with self.assertRaisesRegex(ProjectLifecycleError, "configured principal"):
            provider.authorize_actor(
                "janitor:google-service-account-99999999999999999999:run-abc-1",
                "delete",
                PROJECT_TYPE,
            )

    def test_project_reclaimer_subject_cannot_change_between_retirement_calls(self):
        identity = FakeIdentityTransport()
        transport = FakeTransport([])
        provider = adapter(transport, identity_transport=identity)
        provider.authorize_actor(
            "operator:project-reclaimer", "delete", PROJECT_TYPE
        )
        identity.subject = "98765432109876543210"
        with self.assertRaisesRegex(
            ProjectLifecycleError, "Google subject changed during the run"
        ):
            provider.submit("delete", PROJECT_TYPE, f"projects/{PROJECT}", {})
        self.assertEqual(transport.calls, [])

    def test_project_retirement_fails_closed_without_identity_configuration(self):
        provider = GCPProjectLifecycleAdapter(
            FakeTransport([]),
            FakeTokenSource(),
            project_id=PROJECT,
            run_id=RUN,
            parent=PARENT,
            billing_account_name=BILLING,
            enabled_services=[API],
        )
        with self.assertRaisesRegex(ProjectLifecycleError, "not configured"):
            provider.authorize_actor(
                "operator:project-reclaimer", "delete", PROJECT_TYPE
            )


if __name__ == "__main__":
    unittest.main()
