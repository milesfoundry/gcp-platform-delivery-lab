"""Read-only, exact-ID GCP recovery control verification tests."""

from copy import deepcopy
import json
import unittest

from gcp_recovery_control_verifier import (
    GCPRecoveryControlVerifier,
    RecoveryControlVerificationError,
)
from test_run_contract import intent


RUN = "lab-20261018-a1b2"
PROJECT = "recovery-lab-456"
RUN_PROJECT = "sample-lab-123"
REGION = "europe-west2"
BUCKET = "portfolio-ledger-private"
REAPER = f"{RUN}-reaper@{PROJECT}.iam.gserviceaccount.com"
SCHEDULER = f"{RUN}-sched@{PROJECT}.iam.gserviceaccount.com"
JOB = f"projects/{PROJECT}/locations/{REGION}/jobs/{RUN}-reaper"
SCHEDULE = f"projects/{PROJECT}/locations/{REGION}/jobs/{RUN}-expiry"


class FakeTokenSource:
    def access_token(self):
        return "explicit-test-access-token"


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = {} if payload is None else payload

    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout, allow_redirects):
        self.calls.append({
            "method": method, "url": url, "headers": headers, "body": body,
            "timeout": timeout, "allow_redirects": allow_redirects,
        })
        if not self.responses:
            raise AssertionError("unexpected GCP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response_set():
    role_name = f"projects/{PROJECT}/roles/portfolioLedgerReaper{RUN.replace('-', '')}"
    reaper_sa = {
        "name": f"projects/{PROJECT}/serviceAccounts/{REAPER}",
        "projectId": PROJECT, "email": REAPER,
        "uniqueId": "12345678901234567890", "disabled": False,
        "displayName": f"Portfolio run {RUN} retirement job",
        "description": "Per-run retirement identity. It cannot create projects or recovery controls.",
    }
    scheduler_sa = {
        "name": f"projects/{PROJECT}/serviceAccounts/{SCHEDULER}",
        "projectId": PROJECT, "email": SCHEDULER,
        "uniqueId": "22345678901234567890", "disabled": False,
        "displayName": f"Portfolio run {RUN} Scheduler invocation",
        "description": "May invoke only the matching per-run Cloud Run Job.",
    }
    manifest = intent()
    env = [
        {"name": "PORTFOLIO_RUN_ID", "value": RUN},
        {"name": "PORTFOLIO_PROJECT_ID", "value": RUN_PROJECT},
        {"name": "PORTFOLIO_RECOVERY_PROJECT_ID", "value": PROJECT},
        {"name": "PORTFOLIO_RUN_PARENT", "value": "folders/123456"},
        {"name": "PORTFOLIO_BILLING_ACCOUNT", "value": "billingAccounts/ABCDEF-123456-GHIJKL"},
        {"name": "PORTFOLIO_LEDGER_BUCKET", "value": BUCKET},
        {"name": "PORTFOLIO_ENABLED_SERVICES", "value": json.dumps(manifest["scope"]["enabled_api_services"], separators=(",", ":"))},
        {"name": "PORTFOLIO_REAPER_EMAIL", "value": REAPER},
        {"name": "PORTFOLIO_REAPER_SERVICE_ACCOUNT_ID", "value": reaper_sa["uniqueId"]},
        {"name": "PORTFOLIO_IDENTITY_AUDIENCE", "value": "portfolio-reaper-v1"},
    ]
    job = {
        "name": JOB, "reconciling": False,
        "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
        "template": {
            "taskCount": 1, "parallelism": 1,
            "template": {
                "serviceAccount": REAPER, "timeout": "90s", "maxRetries": 0,
                "containers": [{
                    "image": "ghcr.io/milesfoundry/gcp-reaper@sha256:" + "c" * 64,
                    "env": env,
                }],
            },
        },
    }
    ledger_policy = {
        "version": 3,
        "bindings": [{
            "role": role_name,
            "members": [f"serviceAccount:{REAPER}"],
            "condition": {
                "title": f"Exact manifest for {RUN}",
                "description": "Restrict ledger access to the one durable manifest object.",
                "expression": f"resource.name == 'projects/_/buckets/{BUCKET}/objects/runs/{RUN}/manifest.json'",
            },
        }],
    }
    scheduler = {
        "name": SCHEDULE, "state": "ENABLED", "schedule": "*/5 * * * *",
        "timeZone": "Europe/London", "attemptDeadline": "320s",
        "retryConfig": {"retryCount": 0},
        "httpTarget": {
            "uri": f"https://run.googleapis.com/v2/{JOB}:run",
            "httpMethod": "POST", "body": "e30=",
            "oauthToken": {
                "serviceAccountEmail": SCHEDULER,
                "scope": "https://www.googleapis.com/auth/cloud-platform",
            },
        },
    }
    return manifest, [
        FakeResponse(payload=reaper_sa),
        FakeResponse(payload=scheduler_sa),
        FakeResponse(payload={"projectId": PROJECT, "projectNumber": "123456789"}),
        FakeResponse(payload={
            "name": BUCKET, "projectNumber": "123456789",
            "iamConfiguration": {
                "uniformBucketLevelAccess": {"enabled": True},
                "publicAccessPrevention": "enforced",
            },
        }),
        FakeResponse(payload={
            "name": role_name,
            "title": f"Portfolio {RUN} exact ledger access",
            "description": "Read and replace only this run's manifest object.",
            "stage": "GA", "includedPermissions": [
                "storage.objects.create", "storage.objects.delete", "storage.objects.get",
            ],
        }),
        FakeResponse(payload=ledger_policy),
        FakeResponse(payload=job),
        FakeResponse(payload={"bindings": [{
            "role": "roles/run.invoker", "members": [f"serviceAccount:{SCHEDULER}"],
        }]}),
        FakeResponse(payload=scheduler),
        FakeResponse(status_code=404),
    ]


class GCPRecoveryControlVerifierTests(unittest.TestCase):
    def test_exact_controls_are_verified_with_get_only_and_receipt_digests(self):
        manifest, responses = response_set()
        transport = FakeTransport(responses)
        verifier = GCPRecoveryControlVerifier(transport, FakeTokenSource(), manifest)

        observations = verifier.verify()

        self.assertEqual(len(observations), 4)
        self.assertEqual(
            {(item["type"], item["id"]) for item in observations},
            {(item["type"], item["id"]) for item in manifest["controls"]},
        )
        self.assertTrue(all(len(item["response_sha256"]) == 64 for item in observations))
        self.assertEqual(len(transport.calls), 10)
        self.assertTrue(all(call["method"] == "GET" for call in transport.calls))
        self.assertTrue(all(call["body"] is None for call in transport.calls))
        self.assertTrue(all(call["allow_redirects"] is False for call in transport.calls))

    def test_paused_scheduler_fails_closed(self):
        manifest, responses = response_set()
        scheduler_response = responses[-2]
        scheduler_response.payload["state"] = "PAUSED"
        transport = FakeTransport(responses)
        with self.assertRaisesRegex(RecoveryControlVerificationError, "enabled five-minute"):
            GCPRecoveryControlVerifier(transport, FakeTokenSource(), manifest).verify()

    def test_unreadable_run_project_is_not_treated_as_absent(self):
        manifest, responses = response_set()
        responses[-1] = FakeResponse(status_code=403)
        with self.assertRaisesRegex(RecoveryControlVerificationError, "HTTP 403"):
            GCPRecoveryControlVerifier(FakeTransport(responses), FakeTokenSource(), manifest).verify()

    def test_public_bucket_access_is_rejected_even_when_exact_binding_exists(self):
        manifest, responses = response_set()
        responses[5].payload["bindings"].append({
            "role": "roles/storage.objectViewer", "members": ["allUsers"],
        })
        with self.assertRaisesRegex(RecoveryControlVerificationError, "broader direct access"):
            GCPRecoveryControlVerifier(FakeTransport(responses), FakeTokenSource(), manifest).verify()

    def test_inherited_public_access_prevention_is_not_treated_as_enforced(self):
        manifest, responses = response_set()
        responses[3].payload["iamConfiguration"]["publicAccessPrevention"] = "inherited"
        with self.assertRaisesRegex(RecoveryControlVerificationError, "public access prevention"):
            GCPRecoveryControlVerifier(FakeTransport(responses), FakeTokenSource(), manifest).verify()

    def test_non_deterministic_control_id_is_rejected_before_any_api_call(self):
        manifest, responses = response_set()
        manifest = deepcopy(manifest)
        manifest["controls"][0]["id"] += "-other"
        transport = FakeTransport(responses)
        with self.assertRaisesRegex(RecoveryControlVerificationError, "deterministic run name"):
            GCPRecoveryControlVerifier(transport, FakeTokenSource(), manifest).verify()
        self.assertEqual(transport.calls, [])

    def test_control_response_hash_is_stable_and_does_not_include_access_token(self):
        manifest, responses = response_set()
        first = GCPRecoveryControlVerifier(
            FakeTransport(responses), FakeTokenSource(), manifest
        ).verify()
        manifest, responses = response_set()
        second = GCPRecoveryControlVerifier(
            FakeTransport(responses), FakeTokenSource(), manifest
        ).verify()
        self.assertEqual(first, second)
        self.assertNotIn("explicit-test-access-token", json.dumps(first))
        self.assertTrue(all(
            len(item["response_sha256"]) == 64
            and all(character in "0123456789abcdef" for character in item["response_sha256"])
            for item in first
        ))
