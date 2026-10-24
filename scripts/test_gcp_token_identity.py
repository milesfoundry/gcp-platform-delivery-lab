"""Credential-free tests for Google access-token principal binding."""

import unittest

from gcp_token_identity import (
    CloudRunServiceAccountTokenSource,
    ExplicitGoogleCredentialTokenSource,
    IdentityVerificationError,
    USERINFO_URL,
    verify_access_token_principal,
)


PRINCIPAL = "independent-reader@example.com"


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def json(self):
        return self.body


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, *, params, headers, timeout, allow_redirects):
        self.calls.append((url, params, headers, timeout, allow_redirects))
        return self.response


def user_info(**overrides):
    body = {
        "sub": "12345678901234567890",
        "email": PRINCIPAL,
        "email_verified": True,
    }
    body.update(overrides)
    return body


class GoogleTokenIdentityTests(unittest.TestCase):
    def test_cloud_run_source_binds_api_token_to_verified_service_account_id(self):
        now = 4_000_000_000

        class AccessCredentials:
            service_account_email = "janitor@recovery-project.iam.gserviceaccount.com"
            token = None

            def refresh(self, request):
                self.token = "cloud-run-api-token"

        class IdentityCredentials:
            token = None

            def refresh(self, request):
                self.token = "cloud-run-identity-token"

        claims = {
            "iss": "https://accounts.google.com",
            "aud": "portfolio-reclaimer-v1",
            "email": "janitor@recovery-project.iam.gserviceaccount.com",
            "email_verified": True,
            "sub": "12345678901234567890",
            "azp": "12345678901234567890",
            "exp": now + 60,
        }
        verified = []

        def verify(token, request, audience):
            verified.append((token, request, audience))
            return dict(claims)

        request = object()
        source = CloudRunServiceAccountTokenSource(
            request=request,
            access_credentials=AccessCredentials(),
            identity_credentials=IdentityCredentials(),
            verify_identity_token=verify,
            expected_email="janitor@recovery-project.iam.gserviceaccount.com",
            expected_subject="12345678901234567890",
            audience="portfolio-reclaimer-v1",
        )
        token = source.access_token()
        self.assertEqual(token, "cloud-run-api-token")
        principal = source.verify_principal(token)
        self.assertEqual(principal.email, "janitor@recovery-project.iam.gserviceaccount.com")
        self.assertEqual(principal.auditor_id, "google-service-account-12345678901234567890")
        self.assertEqual(verified, [("cloud-run-identity-token", request, "portfolio-reclaimer-v1")])
        with self.assertRaisesRegex(IdentityVerificationError, "does not match"):
            source.verify_principal("different-api-token")

    def test_cloud_run_source_rejects_wrong_service_account_claims_and_access_identity(self):
        now = 4_000_000_000
        expected_email = "janitor@recovery-project.iam.gserviceaccount.com"
        base_claims = {
            "iss": "https://accounts.google.com",
            "aud": "portfolio-reclaimer-v1",
            "email": expected_email,
            "email_verified": True,
            "sub": "12345678901234567890",
            "azp": "12345678901234567890",
            "exp": now + 60,
        }

        class Credentials:
            def __init__(self, email=expected_email):
                self.service_account_email = email
                self.token = None

            def refresh(self, request):
                self.token = "test-token"

        class IdentityCredentials:
            token = "identity-token"

            def refresh(self, request):
                pass

        invalid_claims = (
            {"aud": "other"},
            {"iss": "https://evil.example"},
            {"email": "other@example.com"},
            {"email_verified": False},
            {"sub": "999"},
            {"azp": "999"},
            {"exp": 1},
        )
        for override in invalid_claims:
            with self.subTest(override=override):
                claims = dict(base_claims)
                claims.update(override)
                source = CloudRunServiceAccountTokenSource(
                    request=object(),
                    access_credentials=Credentials(),
                    identity_credentials=IdentityCredentials(),
                    verify_identity_token=lambda token, request, audience: claims,
                    expected_email=expected_email,
                    expected_subject="12345678901234567890",
                    audience="portfolio-reclaimer-v1",
                )
                with self.assertRaises(IdentityVerificationError):
                    source.access_token()

        source = CloudRunServiceAccountTokenSource(
            request=object(),
            access_credentials=Credentials("other@recovery-project.iam.gserviceaccount.com"),
            identity_credentials=IdentityCredentials(),
            verify_identity_token=lambda token, request, audience: base_claims,
            expected_email=expected_email,
            expected_subject="12345678901234567890",
            audience="portfolio-reclaimer-v1",
        )
        with self.assertRaisesRegex(IdentityVerificationError, "wrong service account"):
            source.access_token()

        def reject_signature(token, request, audience):
            raise RuntimeError("sensitive signature verifier detail")

        invalid_signature_source = CloudRunServiceAccountTokenSource(
            request=object(),
            access_credentials=Credentials(),
            identity_credentials=IdentityCredentials(),
            verify_identity_token=reject_signature,
            expected_email=expected_email,
            expected_subject="12345678901234567890",
            audience="portfolio-reclaimer-v1",
        )
        with self.assertRaises(IdentityVerificationError) as raised:
            invalid_signature_source.access_token()
        self.assertNotIn("sensitive signature verifier detail", str(raised.exception))

    def test_explicit_credential_source_refreshes_without_ambient_discovery(self):
        class Credentials:
            def __init__(self):
                self.valid = False
                self.token = None
                self.calls = []

            def refresh(self, request):
                self.calls.append(request)
                self.valid = True
                self.token = "refreshed-test-token"

        credentials = Credentials()
        refresh_request = object()
        source = ExplicitGoogleCredentialTokenSource(credentials, refresh_request)
        self.assertEqual(source.access_token(), "refreshed-test-token")
        self.assertEqual(source.access_token(), "refreshed-test-token")
        self.assertEqual(credentials.calls, [refresh_request])

    def test_explicit_credential_source_sanitizes_refresh_failures_and_tokens(self):
        class Credentials:
            valid = False
            token = None

            def refresh(self, request):
                raise RuntimeError("sensitive credential detail")

        with self.assertRaises(IdentityVerificationError) as raised:
            ExplicitGoogleCredentialTokenSource(Credentials(), object()).access_token()
        self.assertNotIn("sensitive credential detail", str(raised.exception))

        class InvalidTokenCredentials:
            valid = True
            token = "bad\ntoken"

        with self.assertRaises(IdentityVerificationError):
            ExplicitGoogleCredentialTokenSource(
                InvalidTokenCredentials(), object()
            ).access_token()

    def test_verified_user_subject_is_returned_without_redirects(self):
        transport = FakeTransport(FakeResponse(user_info()))
        principal = verify_access_token_principal(
            transport, "opaque-test-token", expected_email=PRINCIPAL
        )
        self.assertEqual(principal.auditor_id, "google-user-12345678901234567890")
        self.assertEqual(principal.email, PRINCIPAL)
        self.assertEqual(
            transport.calls,
            [(
                USERINFO_URL,
                {},
                {
                    "Authorization": "Bearer opaque-test-token",
                    "Accept": "application/json",
                },
                30,
                False,
            )],
        )

    def test_refuses_mismatched_or_unverified_userinfo(self):
        cases = (
            (user_info(), "other@example.com"),
            (user_info(email_verified=False), PRINCIPAL),
            (user_info(sub=""), PRINCIPAL),
        )
        for body, expected in cases:
            with self.subTest(body=body), self.assertRaises(IdentityVerificationError):
                verify_access_token_principal(
                    FakeTransport(FakeResponse(body)),
                    "opaque-test-token",
                    expected_email=expected,
                )

    def test_refuses_malformed_identity_bad_response_and_failed_userinfo(self):
        for response in (
            FakeResponse({"email": "reader", "email_verified": True,
                          "sub": "12345678901234567890"}),
            FakeResponse(user_info(), status_code=403),
            FakeResponse([user_info()]),
        ):
            with self.subTest(response=response), self.assertRaises(
                IdentityVerificationError
            ):
                verify_access_token_principal(
                    FakeTransport(response),
                    "opaque-test-token",
                    expected_email=PRINCIPAL,
                )


if __name__ == "__main__":
    unittest.main()
