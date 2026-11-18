"""Synthetic negative tests for immutable source and finance evidence guards.

Three reviewed guards in ``run_contract.validate_intent`` are covered here:

* ``source.revision`` must be a full immutable 40-hex Git commit SHA.
* ``source.image_digest`` must be an immutable GHCR ``@sha256:`` digest.
* ``finance.credit_evidence_sha256`` must be a SHA-256 digest.

The donor fixture is imported verbatim from ``test_run_contract``. It is not
copied, edited, or regenerated here, so these cases cannot drift from the
fixture the existing suite already uses.

Every mutation below is rejected by exactly one guard, so removing that guard
makes the expected rejection disappear. The companion harness in the scratch
packet (``tools/negative_guard_mutation.py``) asserts that behaviour. These
tests validate in-memory intent objects only: no credentials, no provider
call, and no network access.
"""

from copy import deepcopy
import hashlib
import json
import unittest

from run_contract import ContractError, validate_intent

# Donor fixture, imported rather than duplicated. ``test_run_contract`` is the
# donor suite; importing it keeps the fixture identical to its source.
from test_run_contract import NOW, intent


# SHA-256 of the canonical donor fixture at the reviewed revision.
DONOR_FIXTURE_SHA256 = (
    "ac593e91036efc1e263d31c3bd14d7559457abda130ffd98bc8a29b96c02d43b"
)
REVISION_MESSAGE = r"^source\.revision must be a full immutable Git SHA$"
IMAGE_MESSAGE = r"^source\.image_digest must be an immutable GHCR digest$"
CREDIT_MESSAGE = r"^finance\.credit_evidence_sha256 must be a SHA-256 digest$"

UNSET = object()

GHCR_PREFIX = "ghcr.io/milesfoundry/report-workshop@sha256:"


def intent_with(*, revision=UNSET, image_digest=UNSET, credit_sha256=UNSET):
    """Return a donor fixture copy with targeted fields overridden."""
    candidate = deepcopy(intent())
    if revision is not UNSET:
        candidate["source"]["revision"] = revision
    if image_digest is not UNSET:
        candidate["source"]["image_digest"] = image_digest
    if credit_sha256 is not UNSET:
        candidate["finance"]["credit_evidence_sha256"] = credit_sha256
    return candidate


class DonorFixtureBindingTests(unittest.TestCase):
    def test_donor_fixture_is_the_recorded_fixture(self):
        canonical = json.dumps(
            intent(), sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            DONOR_FIXTURE_SHA256,
        )

    def test_donor_fixture_satisfies_all_three_guards(self):
        result = validate_intent(intent(), now=NOW)
        self.assertEqual(result["source"]["revision"], "a" * 40)
        self.assertEqual(
            result["source"]["image_digest"], GHCR_PREFIX + "b" * 64
        )
        self.assertEqual(result["finance"]["credit_evidence_sha256"], "d" * 64)


class ImmutableSourceRevisionTests(unittest.TestCase):
    def test_full_immutable_revision_is_accepted(self):
        revision = "0123456789abcdef" * 2 + "01234567"
        candidate = intent_with(revision=revision)
        result = validate_intent(candidate, now=NOW)
        self.assertEqual(result["source"]["revision"], revision)

    def test_revision_mutations_are_rejected(self):
        cases = (
            ("abbreviated", "a" * 7),
            ("one_character_short", "a" * 39),
            ("one_character_long", "a" * 41),
            ("sha256_length", "a" * 64),
            ("uppercase_hex", "A" * 40),
            ("non_hex_character", "a" * 39 + "g"),
            ("branch_name", "refs/heads/main"),
            ("symbolic_ref", "HEAD"),
            ("tag_name", "v1.0.0"),
            ("trailing_newline", "a" * 40 + "\n"),
            ("leading_space", " " + "a" * 40),
        )
        for label, revision in cases:
            with self.subTest(case=label), self.assertRaisesRegex(
                ContractError, REVISION_MESSAGE
            ):
                validate_intent(intent_with(revision=revision), now=NOW)

    def test_revision_mutation_does_not_disturb_other_source_fields(self):
        candidate = intent_with(revision="a" * 7)
        self.assertEqual(
            candidate["source"]["image_digest"], GHCR_PREFIX + "b" * 64
        )


class ImmutableImageDigestTests(unittest.TestCase):
    def test_immutable_image_digest_is_accepted(self):
        digest = GHCR_PREFIX + "1" * 64
        result = validate_intent(intent_with(image_digest=digest), now=NOW)
        self.assertEqual(result["source"]["image_digest"], digest)

    def test_image_digest_mutations_are_rejected(self):
        cases = (
            ("mutable_tag", "ghcr.io/milesfoundry/report-workshop:latest"),
            (
                "floating_branch_tag",
                "ghcr.io/milesfoundry/report-workshop:main",
            ),
            ("digest_separator_dash", GHCR_PREFIX.replace(
                "@sha256:", "@sha256-"
            ) + "b" * 64),
            ("abbreviated_digest", GHCR_PREFIX + "b" * 63),
            ("overlong_digest", GHCR_PREFIX + "b" * 65),
            ("uppercase_digest", GHCR_PREFIX + "B" * 64),
            ("foreign_registry",
             "docker.io/milesfoundry/report-workshop@sha256:" + "b" * 64),
            ("unqualified_image", "report-workshop@sha256:" + "b" * 64),
            ("tag_suffix_after_digest", GHCR_PREFIX + "b" * 64 + ":latest"),
            ("uppercase_repository",
             "ghcr.io/Milesfoundry/report-workshop@sha256:" + "b" * 64),
            ("missing_repository", "ghcr.io/milesfoundry/@sha256:" + "b" * 64),
        )
        for label, digest in cases:
            with self.subTest(case=label), self.assertRaisesRegex(
                ContractError, IMAGE_MESSAGE
            ):
                validate_intent(intent_with(image_digest=digest), now=NOW)

    def test_image_digest_mutation_does_not_disturb_revision(self):
        candidate = intent_with(
            image_digest="ghcr.io/milesfoundry/report-workshop:latest"
        )
        self.assertEqual(candidate["source"]["revision"], "a" * 40)


class FinanceCreditEvidenceTests(unittest.TestCase):
    def test_sha256_credit_evidence_is_accepted(self):
        evidence = "0123456789abcdef" * 4
        candidate = intent_with(credit_sha256=evidence)
        result = validate_intent(candidate, now=NOW)
        self.assertEqual(
            result["finance"]["credit_evidence_sha256"], evidence
        )

    def test_credit_evidence_mutations_are_rejected(self):
        cases = (
            ("abbreviated", "d" * 63),
            ("overlong", "d" * 65),
            ("git_revision_length", "d" * 40),
            ("uppercase_hex", "D" * 64),
            ("non_hex_character", "d" * 63 + "z"),
            ("prefixed_digest", "sha256:" + "d" * 64),
            ("hyphenated_digest",
             "d" * 16 + "-" + "d" * 16 + "-" + "d" * 16 + "-" + "d" * 15),
            ("empty", ""),
            ("absent", None),
            ("non_string_number", 123),
        )
        for label, evidence in cases:
            with self.subTest(case=label), self.assertRaisesRegex(
                ContractError, CREDIT_MESSAGE
            ):
                validate_intent(intent_with(credit_sha256=evidence), now=NOW)

    def test_credit_evidence_mutation_does_not_disturb_source(self):
        candidate = intent_with(credit_sha256="d" * 63)
        self.assertEqual(candidate["source"]["revision"], "a" * 40)
        self.assertEqual(
            candidate["source"]["image_digest"], GHCR_PREFIX + "b" * 64
        )


class GuardIndependenceTests(unittest.TestCase):
    def test_each_guard_rejects_independently_of_the_others(self):
        cases = (
            ("revision", intent_with(revision="a" * 7), REVISION_MESSAGE),
            ("image_digest", intent_with(
                image_digest="ghcr.io/milesfoundry/report-workshop:latest"
            ), IMAGE_MESSAGE),
            ("credit_evidence", intent_with(
                credit_sha256="d" * 63
            ), CREDIT_MESSAGE),
        )
        for label, candidate, message in cases:
            with self.subTest(guard=label), self.assertRaisesRegex(
                ContractError, message
            ):
                validate_intent(candidate, now=NOW)


if __name__ == "__main__":
    unittest.main()
