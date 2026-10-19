"""Mocked transport tests for GET-only GCP closure collection."""

from datetime import datetime, timedelta, timezone
import unittest

from closure_auditor import assess_closure
from gcp_closure_adapter import (
    ClosureCollectionError,
    capture_project_billing,
    capture_run_project_snapshot,
    collect_closure_report,
)
from test_closure_auditor import closure_ready_manifest


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, *, params, headers, timeout, allow_redirects):
        self.calls.append((url, dict(params), dict(headers), timeout, allow_redirects))
        route = self.routes.get(url)
        if not route:
            raise AssertionError(f"unexpected GET {url}")
        if len(route) > 1:
            return route.pop(0)
        return route[0]


def account_response(
    project_id, *, enabled, account="billingAccounts/ABCDEF-123456-GHIJKL"
):
    return FakeResponse(
        200,
        {
            "projectId": project_id,
            "billingEnabled": enabled,
            "billingAccountName": account if enabled else "",
        },
    )


def routes_for(manifest):
    project_id = manifest["scope"]["project_id"]
    recovery = manifest["scope"]["recovery_project_id"]
    account = manifest["finance"]["billing_account_id"]
    return {
        "https://openidconnect.googleapis.com/v1/userinfo": [
            FakeResponse(
                200,
                {
                    "sub": "12345678901234567890",
                    "email": "independent-reader@example.com",
                    "email_verified": True,
                },
            )
        ],
        f"https://cloudbilling.googleapis.com/v1/projects/{project_id}/billingInfo": [
            account_response(project_id, enabled=True),
            account_response(project_id, enabled=False),
        ],
        f"https://cloudresourcemanager.googleapis.com/v3/projects/{project_id}": [
            FakeResponse(200, {"projectId": project_id, "projectNumber": "123456789"}),
            FakeResponse(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        ],
        "https://serviceusage.googleapis.com/v1/projects/123456789/services": [
            FakeResponse(
                200,
                {
                    "services": [
                        {
                            "name": f"projects/123456789/services/{service}",
                            "state": "ENABLED",
                        }
                        for service in manifest["scope"]["enabled_api_services"]
                    ]
                },
            )
        ],
        f"https://iam.googleapis.com/v1/projects/{project_id}/serviceAccounts": [
            FakeResponse(
                200,
                {
                    "accounts": [
                        {
                            "name": (
                                f"projects/{project_id}/serviceAccounts/"
                                "service-123456789@gcp-sa-container.iam."
                                "gserviceaccount.com"
                            )
                        }
                    ]
                },
            )
        ],
        f"https://billingbudgets.googleapis.com/v1/{account}/budgets": [
            FakeResponse(200, {"budgets": []})
        ],
        (
            f"https://run.googleapis.com/v2/projects/{recovery}"
            "/locations/europe-west2/jobs"
        ): [
            FakeResponse(200, {"jobs": []})
        ],
        (
            f"https://cloudscheduler.googleapis.com/v1/projects/{recovery}"
            "/locations/europe-west2/jobs"
        ): [
            FakeResponse(200, {"jobs": []})
        ],
        f"https://iam.googleapis.com/v1/projects/{recovery}/serviceAccounts": [
            FakeResponse(200, {"accounts": []})
        ],
    }


def project_snapshot(manifest):
    project_id = manifest["scope"]["project_id"]
    services = manifest["scope"]["enabled_api_services"]
    accounts = [
        f"projects/{project_id}/serviceAccounts/"
        "service-123456789@gcp-sa-container.iam.gserviceaccount.com"
    ]
    return {
        "project_id": project_id,
        "project_number": "123456789",
        "observed_at": "2026-10-19T01:18:00Z",
        "enabled_services": services,
        "service_accounts": accounts,
        "coverage": {
            "serviceusage.enabled_services": {
                "complete": True,
                "pages": 1,
                "resource_count": len(services),
                "response_sha256": "e" * 64,
            },
            "run_project.service_accounts": {
                "complete": True,
                "pages": 1,
                "resource_count": len(accounts),
                "response_sha256": "f" * 64,
            },
        },
    }


class GcpClosureAdapterTests(unittest.TestCase):
    def test_predeletion_snapshot_binds_project_number_and_paginates_enabled_apis(self):
        manifest = closure_ready_manifest()
        routes = routes_for(manifest)
        service_url = next(
            url for url in routes if "serviceusage.googleapis.com" in url
        )
        services = routes[service_url][0].body["services"]
        routes[service_url] = [
            FakeResponse(200, {"services": services[:5], "nextPageToken": "next"}),
            FakeResponse(200, {"services": services[5:]}),
        ]
        transport = FakeTransport(routes)
        snapshot = capture_run_project_snapshot(
            transport,
            manifest["scope"]["project_id"],
            "explicit-token",
            clock=lambda: datetime(2026, 10, 19, 1, 18, tzinfo=timezone.utc),
        )
        self.assertEqual(snapshot["project_number"], "123456789")
        self.assertEqual(
            snapshot["enabled_services"],
            manifest["scope"]["enabled_api_services"],
        )
        self.assertEqual(
            snapshot["coverage"]["serviceusage.enabled_services"]["pages"], 2
        )
        service_calls = [call for call in transport.calls if call[0] == service_url]
        self.assertEqual(service_calls[0][1]["filter"], "state:ENABLED")
        self.assertEqual(service_calls[1][1]["pageToken"], "next")

    def test_collects_complete_closure_report_using_only_bounded_gets(self):
        manifest = closure_ready_manifest()
        transport = FakeTransport(routes_for(manifest))
        start = datetime(2026, 10, 19, 1, 10, tzinfo=timezone.utc)
        before = capture_project_billing(
            transport,
            manifest["scope"]["project_id"],
            "explicit-test-token",
            observed_at=start,
        )
        after = capture_project_billing(
            transport,
            manifest["scope"]["project_id"],
            "explicit-test-token",
            observed_at=start + timedelta(minutes=5),
        )
        snapshot = capture_run_project_snapshot(
            transport,
            manifest["scope"]["project_id"],
            "explicit-test-token",
            clock=lambda: start + timedelta(minutes=8),
        )
        ticks = iter(start + timedelta(minutes=10 + offset) for offset in range(4))
        report = collect_closure_report(
            manifest,
            billing_before=before,
            billing_after=after,
            project_snapshot=snapshot,
            transport=transport,
            bearer_token="explicit-test-token",
            expected_auditor_email="independent-reader@example.com",
            clock=lambda: next(ticks),
        )
        assessment = assess_closure(manifest, report)
        self.assertEqual(assessment.result, "passed")
        self.assertEqual(len(report["coverage"]), 8)
        self.assertEqual(len(transport.calls), 11)
        for url, _params, headers, timeout, allow_redirects in transport.calls:
            self.assertTrue(url.startswith("https://"))
            self.assertEqual(timeout, 30)
            self.assertFalse(allow_redirects)
            if url == "https://openidconnect.googleapis.com/v1/userinfo":
                self.assertEqual(_params, {})
                self.assertEqual(headers["Authorization"], "Bearer explicit-test-token")
            else:
                self.assertEqual(headers["Authorization"], "Bearer explicit-test-token")

    def test_paginates_project_scoped_budgets_and_retains_response_fingerprints(self):
        manifest = closure_ready_manifest()
        routes = routes_for(manifest)
        budget_url = next(
            url for url in routes if "billingbudgets.googleapis.com" in url
        )
        routes[budget_url] = [
            FakeResponse(200, {"budgets": [], "nextPageToken": "page-2"}),
            FakeResponse(200, {"budgets": []}),
        ]
        transport = FakeTransport(routes)
        before = capture_project_billing(
            transport, manifest["scope"]["project_id"], "token",
            observed_at=datetime(2026, 10, 19, 1, 10, tzinfo=timezone.utc),
        )
        after = capture_project_billing(
            transport, manifest["scope"]["project_id"], "token",
            observed_at=datetime(2026, 10, 19, 1, 15, tzinfo=timezone.utc),
        )
        snapshot = capture_run_project_snapshot(
            transport,
            manifest["scope"]["project_id"],
            "token",
            clock=lambda: datetime(2026, 10, 19, 1, 18, tzinfo=timezone.utc),
        )
        start = datetime(2026, 10, 19, 1, 20, tzinfo=timezone.utc)
        ticks = iter(start + timedelta(seconds=offset) for offset in range(4))
        report = collect_closure_report(
            manifest,
            billing_before=before,
            billing_after=after,
            project_snapshot=snapshot,
            transport=transport,
            bearer_token="token",
            expected_auditor_email="independent-reader@example.com",
            clock=lambda: next(ticks),
        )
        self.assertEqual(report["coverage"]["billing.budgets"]["pages"], 2)
        self.assertEqual(report["budgets"], [])
        budget_calls = [call for call in transport.calls if call[0] == budget_url]
        self.assertEqual(
            budget_calls[0][1]["scope"],
            f"projects/{manifest['scope']['project_id']}",
        )
        self.assertEqual(budget_calls[1][1]["pageToken"], "page-2")

    def test_rejects_missing_token_failed_page_and_wrong_project(self):
        manifest = closure_ready_manifest()
        transport = FakeTransport(routes_for(manifest))
        with self.assertRaises(ClosureCollectionError):
            capture_project_billing(
                transport,
                manifest["scope"]["project_id"],
                "",
                observed_at=datetime(2026, 10, 19, 1, 10, tzinfo=timezone.utc),
            )

        transport = FakeTransport(routes_for(manifest))
        project_url = next(
            url for url in transport.routes if "cloudresourcemanager" in url
        )
        transport.routes[project_url] = [FakeResponse(500, {"error": "denied"})]
        before = {
            "project_id": manifest["scope"]["project_id"],
            "billing_enabled": True,
            "billing_account_id": manifest["finance"]["billing_account_id"],
            "observed_at": "2026-10-19T01:10:00Z",
            "response_sha256": "a" * 64,
        }
        after = {
            "project_id": manifest["scope"]["project_id"],
            "billing_enabled": False,
            "billing_account_id": None,
            "observed_at": "2026-10-19T01:15:00Z",
            "response_sha256": "b" * 64,
        }
        with self.assertRaises(ClosureCollectionError):
            collect_closure_report(
                manifest,
                billing_before=before,
                billing_after=after,
                project_snapshot=project_snapshot(manifest),
                transport=transport,
                bearer_token="token",
                expected_auditor_email="independent-reader@example.com",
            )

        transport = FakeTransport(routes_for(manifest))
        billing_url = next(
            url for url in transport.routes if "cloudbilling.googleapis.com" in url
        )
        transport.routes[billing_url] = [
            FakeResponse(
                200,
                {"projectId": "other-project", "billingEnabled": True},
            )
        ]
        with self.assertRaises(ClosureCollectionError):
            capture_project_billing(
                transport,
                manifest["scope"]["project_id"],
                "token",
                observed_at=datetime(2026, 10, 19, 1, 10, tzinfo=timezone.utc),
            )

    def test_rejects_wrong_auditor_and_malformed_control_inventory(self):
        manifest = closure_ready_manifest()
        transport = FakeTransport(routes_for(manifest))
        before = capture_project_billing(
            transport,
            manifest["scope"]["project_id"],
            "token",
            observed_at=datetime(2026, 10, 19, 1, 10, tzinfo=timezone.utc),
        )
        after = capture_project_billing(
            transport,
            manifest["scope"]["project_id"],
            "token",
            observed_at=datetime(2026, 10, 19, 1, 15, tzinfo=timezone.utc),
        )
        snapshot = project_snapshot(manifest)
        with self.assertRaises(ClosureCollectionError):
            collect_closure_report(
                manifest,
                billing_before=before,
                billing_after=after,
                project_snapshot=snapshot,
                transport=transport,
                bearer_token="token",
                expected_auditor_email="bad space",
            )

        transport = FakeTransport(routes_for(manifest))
        before = capture_project_billing(
            transport,
            manifest["scope"]["project_id"],
            "token",
            observed_at=datetime(2026, 10, 19, 1, 10, tzinfo=timezone.utc),
        )
        after = capture_project_billing(
            transport,
            manifest["scope"]["project_id"],
            "token",
            observed_at=datetime(2026, 10, 19, 1, 15, tzinfo=timezone.utc),
        )
        snapshot = capture_run_project_snapshot(
            transport,
            manifest["scope"]["project_id"],
            "token",
            clock=lambda: datetime(2026, 10, 19, 1, 18, tzinfo=timezone.utc),
        )
        run_url = next(url for url in transport.routes if "run.googleapis.com" in url)
        transport.routes[run_url] = [
            FakeResponse(
                200,
                {
                    "jobs": [{
                        "name": "projects/outside/locations/europe-west2/jobs/x"
                    }]
                },
            )
        ]
        with self.assertRaises(ClosureCollectionError):
            collect_closure_report(
                manifest,
                billing_before=before,
                billing_after=after,
                project_snapshot=snapshot,
                transport=transport,
                bearer_token="token",
                expected_auditor_email="independent-reader@example.com",
            )

    def test_refuses_mismatched_token_principal_before_closure_inventory(self):
        manifest = closure_ready_manifest()
        routes = routes_for(manifest)
        routes["https://openidconnect.googleapis.com/v1/userinfo"] = [
            FakeResponse(
                200,
                {
                    "sub": "99999999999999999999",
                    "email": "someone-else@example.com",
                    "email_verified": True,
                },
            )
        ]
        transport = FakeTransport(routes)
        before = {
            "project_id": manifest["scope"]["project_id"],
            "billing_enabled": True,
            "billing_account_id": manifest["finance"]["billing_account_id"],
            "observed_at": "2026-10-19T01:10:00Z",
            "response_sha256": "a" * 64,
        }
        after = {
            "project_id": manifest["scope"]["project_id"],
            "billing_enabled": False,
            "billing_account_id": None,
            "observed_at": "2026-10-19T01:15:00Z",
            "response_sha256": "b" * 64,
        }
        with self.assertRaises(ClosureCollectionError):
            collect_closure_report(
                manifest,
                billing_before=before,
                billing_after=after,
                project_snapshot=project_snapshot(manifest),
                transport=transport,
                bearer_token="token",
                expected_auditor_email="independent-reader@example.com",
            )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(
            transport.calls[0][0], "https://openidconnect.googleapis.com/v1/userinfo"
        )


if __name__ == "__main__":
    unittest.main()
