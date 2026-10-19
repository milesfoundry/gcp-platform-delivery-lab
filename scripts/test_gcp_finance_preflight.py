"""Fake-transport qualification for read-only GCP finance preflight."""

from datetime import datetime, timedelta, timezone
import unittest

from gcp_finance_preflight import (
    EXPECTED_THRESHOLDS,
    FinancePreflightError,
    GCPFinancePreflight,
)
from test_run_contract import intent


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
EMAIL = "finance-operator@example.com"
SUBJECT = "google-user-1234567890"
TOKEN = "explicit-short-lived-token"


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeTokenSource:
    def access_token(self):
        return TOKEN


class FakeIdentityTransport:
    def __init__(self, subject="1234567890", email=EMAIL):
        self.subject = subject
        self.email = email

    def get(self, url, **kwargs):
        return FakeResponse(200, {
            "sub": self.subject,
            "email": self.email,
            "email_verified": True,
        })


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method != "GET":
            raise AssertionError("finance preflight must be read-only")
        return self.routes.get(url, FakeResponse(404, {}))


def make_manifest():
    manifest = intent()
    manifest["created_at"] = "2026-10-19T11:00:00Z"
    manifest["deadline_at"] = "2026-10-19T11:50:00Z"
    manifest["finance"]["credit_observed_at"] = "2026-10-19T10:55:00Z"
    manifest["finance"]["credit_expires_at"] = "2026-11-19T00:00:00Z"
    manifest["finance"]["alert_delivery_verified_at"] = "2026-10-19T10:30:00Z"
    manifest["state"] = "prepared"
    return manifest


def make_budget(manifest, *, project_number="123456789012"):
    rules = [
        {"thresholdPercent": percent, "spendBasis": basis}
        for percent, basis in sorted(EXPECTED_THRESHOLDS)
    ]
    return {
        "name": f"{manifest['finance']['billing_account_id']}/budgets/unique-run-budget",
        "displayName": "Ephemeral portfolio lab gross cost",
        "budgetFilter": {
            "projects": [f"projects/{project_number}"],
            "creditTypesTreatment": "EXCLUDE_ALL_CREDITS",
            "calendarPeriod": "MONTH",
        },
        "amount": {
            "specifiedAmount": {
                "currencyCode": "GBP", "units": "70", "nanos": 0,
            }
        },
        "thresholdRules": rules,
        "notificationsRule": {
            "monitoringNotificationChannels": [
                manifest["finance"]["alert_notification_channel"]
            ],
            "disableDefaultIamRecipients": True,
        },
    }


def make_preflight(manifest, *, budgets=None, project=None, identity=None):
    project = project or {
        "projectId": manifest["scope"]["project_id"],
        "projectNumber": "123456789012",
        "displayName": manifest["run_id"],
        "parent": manifest["scope"]["parent"],
        "state": "ACTIVE",
        "labels": {"lab_run_id": manifest["run_id"]},
    }
    account = manifest["finance"]["billing_account_id"]
    project_id = manifest["scope"]["project_id"]
    routes = {
        f"https://cloudresourcemanager.googleapis.com/v3/projects/{project_id}": FakeResponse(200, project),
        f"https://cloudbilling.googleapis.com/v1/projects/{project_id}/billingInfo": FakeResponse(200, {
            "projectId": project_id,
            "billingEnabled": True,
            "billingAccountName": account,
        }),
        f"https://billingbudgets.googleapis.com/v1/{account}/budgets?pageSize=100": FakeResponse(200, {
            "budgets": budgets if budgets is not None else [make_budget(manifest)],
        }),
        "https://monitoring.googleapis.com/v3/"
        + manifest["finance"]["alert_notification_channel"]: FakeResponse(200, {
            "name": manifest["finance"]["alert_notification_channel"],
            "type": "email",
            "enabled": True,
            "verificationStatus": "VERIFIED",
            "labels": {"email_address": "private@example.com"},
        }),
    }
    return GCPFinancePreflight(
        FakeTransport(routes),
        FakeTokenSource(),
        identity or FakeIdentityTransport(),
        expected_email=EMAIL,
        expected_subject=SUBJECT,
    )


class FinancePreflightTests(unittest.TestCase):
    def test_fresh_operator_evidence_and_exact_budget_produce_ledger_receipt(self):
        manifest = make_manifest()
        preflight = make_preflight(manifest)

        proof = preflight.verify_project_budget(manifest, observed_at=NOW)

        self.assertEqual(proof["verified_by"], SUBJECT)
        self.assertEqual(proof["project_number"], "123456789012")
        self.assertTrue(proof["budget_response_sha256"])
        self.assertEqual(len(preflight._transport.calls), 4)
        self.assertTrue(all(call[0] == "GET" for call in preflight._transport.calls))

    def test_missing_project_budget_pauses_without_mutation(self):
        manifest = make_manifest()
        preflight = make_preflight(manifest, budgets=[])

        self.assertIsNone(preflight.verify_project_budget(manifest, observed_at=NOW))
        self.assertTrue(all(call[0] == "GET" for call in preflight._transport.calls))

    def test_stale_credit_evidence_is_rejected_before_cloud_reads(self):
        manifest = make_manifest()
        manifest["finance"]["credit_observed_at"] = (
            NOW - timedelta(hours=7)
        ).isoformat()
        preflight = make_preflight(manifest)

        with self.assertRaisesRegex(FinancePreflightError, "credit observation is stale"):
            preflight.verify_project_budget(manifest, observed_at=NOW)
        self.assertEqual(preflight._transport.calls, [])

    def test_stale_alert_delivery_evidence_is_rejected(self):
        manifest = make_manifest()
        manifest["finance"]["alert_delivery_verified_at"] = (
            NOW - timedelta(hours=25)
        ).isoformat()

        with self.assertRaisesRegex(FinancePreflightError, "alert delivery evidence is stale"):
            make_preflight(manifest).verify(manifest, observed_at=NOW)

    def test_nonzero_projected_cash_exposure_requires_separate_authorization(self):
        manifest = make_manifest()
        manifest["finance"]["net_cash_exposure_gbp"] = 0.01

        with self.assertRaisesRegex(FinancePreflightError, "separate explicit spend authorization"):
            make_preflight(manifest).verify(manifest, observed_at=NOW)

    def test_credit_must_cover_gross_estimate(self):
        manifest = make_manifest()
        manifest["finance"]["available_credit_gbp"] = 1

        with self.assertRaisesRegex(FinancePreflightError, "does not cover the gross run estimate"):
            make_preflight(manifest).verify(manifest, observed_at=NOW)

    def test_budget_must_use_the_exact_reviewed_email_channel(self):
        manifest = make_manifest()
        budget = make_budget(manifest)
        budget["notificationsRule"]["monitoringNotificationChannels"] = [
            "projects/other-recovery/notificationChannels/456"
        ]

        with self.assertRaisesRegex(FinancePreflightError, "exact reviewed recovery-project channel"):
            make_preflight(manifest, budgets=[budget]).verify_project_budget(
                manifest, observed_at=NOW
            )

    def test_budget_must_not_add_unreviewed_recipients_or_pubsub_routes(self):
        for field, value in (
            ("enableProjectLevelRecipients", True),
            ("pubsubTopic", "projects/recovery-lab-456/topics/unreviewed"),
        ):
            manifest = make_manifest()
            budget = make_budget(manifest)
            budget["notificationsRule"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                FinancePreflightError, "exact reviewed recovery-project channel"
            ):
                make_preflight(manifest, budgets=[budget]).verify_project_budget(
                    manifest, observed_at=NOW
                )

    def test_email_channel_must_be_enabled_and_verified(self):
        manifest = make_manifest()
        preflight = make_preflight(manifest)
        channel_url = (
            "https://monitoring.googleapis.com/v3/"
            + manifest["finance"]["alert_notification_channel"]
        )
        preflight._transport.routes[channel_url] = FakeResponse(200, {
            "name": manifest["finance"]["alert_notification_channel"],
            "type": "email",
            "enabled": True,
            "verificationStatus": "UNVERIFIED",
        })

        with self.assertRaisesRegex(FinancePreflightError, "missing, disabled, or unverified"):
            preflight.verify_project_budget(manifest, observed_at=NOW)

    def test_project_identity_must_match_exact_manifest(self):
        manifest = make_manifest()
        project = {
            "projectId": "different-project",
            "projectNumber": "123456789012",
            "displayName": manifest["run_id"],
            "parent": manifest["scope"]["parent"],
            "state": "ACTIVE",
            "labels": {"lab_run_id": manifest["run_id"]},
        }

        with self.assertRaisesRegex(FinancePreflightError, "project identity"):
            make_preflight(manifest, project=project).verify_project_budget(
                manifest, observed_at=NOW
            )

    def test_duplicate_project_scoped_budgets_require_operator_reconciliation(self):
        manifest = make_manifest()
        budgets = [make_budget(manifest), make_budget(manifest)]

        with self.assertRaisesRegex(FinancePreflightError, "multiple billing budgets"):
            make_preflight(manifest, budgets=budgets).verify_project_budget(
                manifest, observed_at=NOW
            )

    def test_budget_must_filter_gross_cost_to_exact_project(self):
        manifest = make_manifest()
        budget = make_budget(manifest)
        budget["budgetFilter"]["projects"] = ["projects/999999"]

        self.assertIsNone(
            make_preflight(manifest, budgets=[budget]).verify_project_budget(
                manifest, observed_at=NOW
            )
        )

    def test_budget_must_cover_estimate_with_expected_gbp_alerts(self):
        manifest = make_manifest()
        budget = make_budget(manifest)
        budget["amount"]["specifiedAmount"]["units"] = "4"

        with self.assertRaisesRegex(FinancePreflightError, "match the reviewed threshold"):
            make_preflight(manifest, budgets=[budget]).verify_project_budget(
                manifest, observed_at=NOW
            )

    def test_wrong_user_subject_is_rejected(self):
        manifest = make_manifest()
        identity = FakeIdentityTransport(subject="different-user")

        with self.assertRaisesRegex(FinancePreflightError, "principal"):
            make_preflight(manifest, identity=identity).verify_project_budget(
                manifest, observed_at=NOW
            )

    def test_network_failure_fails_closed_without_response_detail(self):
        class BrokenTransport:
            def request(self, *args, **kwargs):
                raise RuntimeError(TOKEN)

        manifest = make_manifest()
        preflight = GCPFinancePreflight(
            BrokenTransport(), FakeTokenSource(), FakeIdentityTransport(),
            expected_email=EMAIL, expected_subject=SUBJECT,
        )
        with self.assertRaises(FinancePreflightError) as caught:
            preflight.verify_project_budget(manifest, observed_at=NOW)
        self.assertNotIn(TOKEN, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
