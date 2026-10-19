"""Mocked transport tests for GET-only GCP closure collection."""

from datetime import datetime, timedelta, timezone
import unittest

from closure_auditor import assess_closure
from gcp_closure_adapter import (
    ClosureCollectionError,
    capture_project_billing,
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
        f"https://cloudbilling.googleapis.com/v1/projects/{project_id}/billingInfo": [
            account_response(project_id, enabled=True),
            account_response(project_id, enabled=False),
        ],
        f"https://cloudresourcemanager.googleapis.com/v3/projects/{project_id}": [
            FakeResponse(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
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


class GcpClosureAdapterTests(unittest.TestCase):
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
        ticks = iter(start + timedelta(minutes=10 + offset) for offset in range(4))
        report = collect_closure_report(
            manifest,
            billing_before=before,
            billing_after=after,
            transport=transport,
            bearer_token="explicit-test-token",
            auditor_id="independent-reader",
            clock=lambda: next(ticks),
        )
        assessment = assess_closure(manifest, report)
        self.assertEqual(assessment.result, "passed")
        self.assertEqual(len(report["coverage"]), 6)
        self.assertEqual(len(transport.calls), 7)
        for url, _params, headers, timeout, allow_redirects in transport.calls:
            self.assertTrue(url.startswith("https://"))
            self.assertEqual(headers["Authorization"], "Bearer explicit-test-token")
            self.assertEqual(timeout, 30)
            self.assertFalse(allow_redirects)

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
        start = datetime(2026, 10, 19, 1, 20, tzinfo=timezone.utc)
        ticks = iter(start + timedelta(seconds=offset) for offset in range(4))
        report = collect_closure_report(
            manifest,
            billing_before=before,
            billing_after=after,
            transport=transport,
            bearer_token="token",
            auditor_id="independent-reader",
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
            "billing_enabled": True,
            "billing_account_id": manifest["finance"]["billing_account_id"],
            "observed_at": "2026-10-19T01:10:00Z",
            "response_sha256": "a" * 64,
        }
        after = {
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
                transport=transport,
                bearer_token="token",
                auditor_id="independent-reader",
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
        with self.assertRaises(ClosureCollectionError):
            collect_closure_report(
                manifest,
                billing_before=before,
                billing_after=after,
                transport=transport,
                bearer_token="token",
                auditor_id="bad space",
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
                transport=transport,
                bearer_token="token",
                auditor_id="independent-reader",
            )


if __name__ == "__main__":
    unittest.main()
