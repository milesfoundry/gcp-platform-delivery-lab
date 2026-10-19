"""Fake-transport checks for exact-generation GCS state recovery copies."""

import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from gcp_state_backup_adapter import GCPStateBackupAdapter, StateBackupError
from test_run_contract import intent


class Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class Token:
    def access_token(self):
        return "explicit-test-token"


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.bucket_metadata = {}
        self.objects = {}
        self.object_versions = []
        self.rewrite_results = []

    def request(self, method, url, *, headers, body, timeout, allow_redirects):
        self.calls.append((method, url, headers, body, timeout, allow_redirects))
        parsed = urlsplit(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET" and path.endswith("/o") and path.startswith("/storage/v1/b/"):
            return Response(200, {"items": self.object_versions})
        if path.startswith("/storage/v1/b/") and "/o/" not in path:
            bucket = unquote(path.split("/b/", 1)[1])
            return Response(200, self.bucket_metadata[bucket])
        if path.startswith("/v3/projects/"):
            project_id = unquote(path.split("/v3/projects/", 1)[1])
            number = {"recovery-lab-456": "456", "portfolio-state-789": "789"}[project_id]
            return Response(200, {
                "name": f"projects/{number}", "projectId": project_id, "state": "ACTIVE",
            })
        if "/rewriteTo/" in path:
            if not self.rewrite_results:
                raise AssertionError("unexpected rewrite request")
            return Response(200, self.rewrite_results.pop(0))
        if method == "GET" and "/o/" in path:
            bucket, object_name = self._object_path(path)
            generation = query.get("generation", [None])[0]
            if generation is None:
                matches = [
                    (int(candidate_generation), value)
                    for (
                        candidate_bucket, candidate_object, candidate_generation
                    ), value in self.objects.items()
                    if candidate_bucket == bucket and candidate_object == object_name
                ]
                if not matches:
                    return Response(404, {})
                return Response(200, max(matches, key=lambda item: item[0])[1])
            return Response(200, self.objects[(bucket, object_name, generation)])
        raise AssertionError(f"unexpected request {method} {url}")

    @staticmethod
    def _object_path(path):
        head, tail = path.split("/o/", 1)
        return unquote(head.split("/b/", 1)[1]), unquote(tail)


def secure_bucket(name, number):
    return {
        "name": name,
        "projectNumber": number,
        "iamConfiguration": {
            "uniformBucketLevelAccess": {"enabled": True},
            "publicAccessPrevention": "enforced",
        },
        "versioning": {"enabled": True},
        "retentionPolicy": {"retentionPeriod": "604800", "isLocked": False},
    }


class StateBackupAdapterTests(unittest.TestCase):
    def setUp(self):
        self.manifest = intent()
        self.transport = FakeTransport()
        self.transport.bucket_metadata = {
            "run-state-lab-20261018-a1b2": secure_bucket("run-state-lab-20261018-a1b2", "456"),
            "portfolio-state-backup": secure_bucket("portfolio-state-backup", "789"),
        }
        self.source = {
            "bucket": "run-state-lab-20261018-a1b2",
            "name": "runs/lab-20261018-a1b2/terraform.tfstate",
            "generation": "12", "crc32c": "ImIEBA==", "size": "314",
        }
        self.transport.objects[(self.source["bucket"], self.source["name"], "12")] = self.source
        self.transport.object_versions = [self.source]
        self.adapter = GCPStateBackupAdapter(self.transport, Token(), self.manifest)

    def test_backup_copies_exact_source_generation_with_destination_cas(self):
        self.transport.rewrite_results.append({"done": False, "rewriteToken": "rewrite-next"})
        self.transport.rewrite_results.append({
            "done": True,
            "resource": {
                "bucket": "portfolio-state-backup",
                "name": "runs/lab-20261018-a1b2/terraform.tfstate",
                "generation": "25", "crc32c": "ImIEBA==", "size": "314",
            },
        })
        result = self.adapter.backup("12")
        self.assertEqual(result.destination_generation, "25")
        self.assertEqual(result.crc32c, "ImIEBA==")
        call = next(call for call in self.transport.calls if "/rewriteTo/" in call[1])
        self.assertEqual(call[0], "POST")
        self.assertFalse(call[5])
        query = parse_qs(urlsplit(call[1]).query)
        self.assertEqual(query["sourceGeneration"], ["12"])
        self.assertEqual(query["ifSourceGenerationMatch"], ["12"])
        self.assertEqual(query["ifGenerationMatch"], ["0"])
        self.assertIn(b"portfolioSourceGeneration", call[3])
        rewrite_calls = [call for call in self.transport.calls if "/rewriteTo/" in call[1]]
        self.assertEqual(len(rewrite_calls), 2)
        continuation = parse_qs(urlsplit(rewrite_calls[1][1]).query)
        self.assertEqual(continuation, {"rewriteToken": ["rewrite-next"]})
        self.assertIsNone(rewrite_calls[1][3])

    def test_restore_targets_new_object_and_uses_exact_backup_generation(self):
        backup_object = {
            "bucket": "portfolio-state-backup",
            "name": "runs/lab-20261018-a1b2/terraform.tfstate",
            "generation": "25", "crc32c": "ImIEBA==", "size": "314",
        }
        self.transport.objects[
            (backup_object["bucket"], backup_object["name"], "25")
        ] = backup_object
        self.transport.rewrite_results.append({
            "done": True,
            "resource": {
                "bucket": "run-state-lab-20261018-a1b2",
                "name": "runs/lab-20261018-a1b2/restore-proofs/current-12.tfstate",
                "generation": "33", "crc32c": "ImIEBA==", "size": "314",
            },
        })
        result = self.adapter.restore_to_new_object(
            source_generation="12", backup_generation="25", expected_crc32c="ImIEBA==",
            object_name="runs/lab-20261018-a1b2/restore-proofs/current-12.tfstate",
        )
        self.assertEqual(result.destination_generation, "33")
        call = next(call for call in self.transport.calls if "/rewriteTo/" in call[1])
        query = parse_qs(urlsplit(call[1]).query)
        self.assertEqual(query["sourceGeneration"], ["25"])
        self.assertEqual(query["ifSourceGenerationMatch"], ["25"])
        self.assertEqual(query["ifGenerationMatch"], ["0"])
        self.assertIn("restore-proofs/current-12.tfstate", unquote(call[1]))

    def test_rejects_active_backend_or_backup_as_restore_destination_before_writes(self):
        for name in (
            "runs/lab-20261018-a1b2/terraform.tfstate",
            "runs/lab-20261018-a1b2/restore-proofs/../../terraform.tfstate",
        ):
            with self.assertRaises(StateBackupError):
                self.adapter.restore_to_new_object(
                    source_generation="12", backup_generation="25",
                    expected_crc32c="ImIEBA==", object_name=name,
                )
        self.assertFalse(any("/rewriteTo/" in call[1] for call in self.transport.calls))

    def test_rejects_bucket_without_versioning_before_object_access(self):
        self.transport.bucket_metadata["portfolio-state-backup"]["versioning"]["enabled"] = False
        with self.assertRaises(StateBackupError):
            self.adapter.backup("12")
        self.assertFalse(any("/o/" in call[1] for call in self.transport.calls))

    def test_rejects_checksum_mismatch_before_restore_copy(self):
        backup_object = {
            "bucket": "portfolio-state-backup",
            "name": "runs/lab-20261018-a1b2/terraform.tfstate",
            "generation": "25", "crc32c": "wrong==", "size": "314",
        }
        self.transport.objects[
            (backup_object["bucket"], backup_object["name"], "25")
        ] = backup_object
        with self.assertRaises(StateBackupError):
            self.adapter.restore_to_new_object(
                source_generation="12", backup_generation="25",
                expected_crc32c="ImIEBA==",
                object_name="runs/lab-20261018-a1b2/restore-proofs/current-12.tfstate",
            )
        self.assertFalse(any("/rewriteTo/" in call[1] for call in self.transport.calls))

    def test_backup_reconciles_a_committed_copy_after_response_loss(self):
        copied = {
            "bucket": "portfolio-state-backup",
            "name": "runs/lab-20261018-a1b2/terraform.tfstate",
            "generation": "25", "crc32c": "ImIEBA==", "size": "314",
            "metadata": {
                "portfolioRunId": "lab-20261018-a1b2",
                "portfolioCopyPurpose": "state-backup",
                "portfolioSourceGeneration": "12",
            },
        }
        self.transport.objects[(copied["bucket"], copied["name"], "25")] = copied
        result = self.adapter.backup("12")
        self.assertEqual(result.destination_generation, "25")
        self.assertTrue(any(
            call[0] == "GET"
            and "fields=bucket%2Cname%2Cgeneration%2Ccrc32c%2Csize%2Cmetadata" in call[1]
            for call in self.transport.calls
        ))

    def test_backup_does_not_reconcile_a_different_runs_copy(self):
        copied = {
            "bucket": "portfolio-state-backup",
            "name": "runs/lab-20261018-a1b2/terraform.tfstate",
            "generation": "25", "crc32c": "ImIEBA==", "size": "314",
            "metadata": {
                "portfolioRunId": "another-run",
                "portfolioCopyPurpose": "state-backup",
                "portfolioSourceGeneration": "12",
            },
        }
        self.transport.objects[(copied["bucket"], copied["name"], "25")] = copied
        with self.assertRaises(StateBackupError):
            self.adapter.backup("12")

    def test_restore_reconciles_a_committed_recovery_copy_after_response_loss(self):
        backup = {
            "bucket": "portfolio-state-backup",
            "name": "runs/lab-20261018-a1b2/terraform.tfstate",
            "generation": "25", "crc32c": "ImIEBA==", "size": "314",
        }
        restored = {
            "bucket": "run-state-lab-20261018-a1b2",
            "name": "runs/lab-20261018-a1b2/restore-proofs/current-12.tfstate",
            "generation": "33", "crc32c": "ImIEBA==", "size": "314",
            "metadata": {
                "portfolioRunId": "lab-20261018-a1b2",
                "portfolioCopyPurpose": "state-restore-proof",
                "portfolioSourceGeneration": "12",
                "portfolioBackupGeneration": "25",
            },
        }
        self.transport.objects[(backup["bucket"], backup["name"], "25")] = backup
        self.transport.objects[(restored["bucket"], restored["name"], "33")] = restored
        result = self.adapter.restore_to_new_object(
            source_generation="12", backup_generation="25",
            expected_crc32c="ImIEBA==", object_name=restored["name"],
        )
        self.assertEqual(result.destination_generation, "33")
        self.assertEqual(result.source_generation, "12")

    def test_qualifies_current_and_previous_state_generations(self):
        previous = {
            "bucket": "run-state-lab-20261018-a1b2",
            "name": "runs/lab-20261018-a1b2/terraform.tfstate",
            "generation": "8", "crc32c": "ImIEBA==", "size": "300",
        }
        self.transport.objects[(previous["bucket"], previous["name"], "8")] = previous
        self.transport.object_versions = [self.source, previous]
        for generation, destination_generation in (("8", "25"), ("12", "26")):
            self.transport.rewrite_results.append({
                "done": True,
                "resource": {
                    "bucket": "portfolio-state-backup",
                    "name": "runs/lab-20261018-a1b2/terraform.tfstate",
                    "generation": destination_generation,
                    "crc32c": "ImIEBA==",
                    "size": "300" if generation == "8" else "314",
                },
            })
        for label, source_generation, backup_generation, restored_generation in (
            ("current", "12", "26", "27"),
            ("previous", "8", "25", "28"),
        ):
            restore_name = (
                f"runs/lab-20261018-a1b2/restore-proofs/"
                f"{label}-{source_generation}.tfstate"
            )
            self.transport.objects[(
                "portfolio-state-backup", self.source["name"], backup_generation
            )] = {
                "bucket": "portfolio-state-backup", "name": self.source["name"],
                "generation": backup_generation, "crc32c": "ImIEBA==",
                "size": "300" if source_generation == "8" else "314",
            }
            self.transport.rewrite_results.append({
                "done": True,
                "resource": {
                    "bucket": "run-state-lab-20261018-a1b2", "name": restore_name,
                    "generation": restored_generation, "crc32c": "ImIEBA==",
                    "size": "300" if source_generation == "8" else "314",
                },
            })
        proofs = self.adapter.backup_and_restore_latest_two()
        self.assertEqual(proofs["current"]["state_generation"], "12")
        self.assertEqual(proofs["previous"]["state_generation"], "8")
        self.assertEqual(proofs["current"]["backup_generation"], "26")
        self.assertEqual(proofs["previous"]["backup_generation"], "25")
        self.assertEqual(proofs["current"]["restored_generation"], "27")
        self.assertEqual(proofs["previous"]["restored_generation"], "28")


if __name__ == "__main__":
    unittest.main()
