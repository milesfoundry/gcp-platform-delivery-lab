"""Fail-closed Google user and Cloud Run service-account identity binding.

Human access tokens are checked through UserInfo. Scheduled Cloud Run jobs use
the configured service account and a Google-signed ID token from the metadata
server; user refresh tokens are not used by the job runtime.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlsplit


class IdentityVerificationError(RuntimeError):
    """A token did not prove the requested Google principal."""


class Response(Protocol):
    status_code: int

    def json(self) -> object: ...


class IdentityTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        params: dict,
        headers: dict,
        timeout: int,
        allow_redirects: bool,
    ) -> Response: ...


class AccessTokenSource(Protocol):
    """Source of short-lived Google OAuth access tokens."""

    def access_token(self) -> str: ...


class ExplicitGoogleCredentialTokenSource:
    """Refresh and expose an explicitly supplied Google auth credential.

    The credential and its refresh transport must be constructed by a trusted
    caller. This wrapper performs no ADC lookup, file discovery, or login.
    Principal checks remain the responsibility of
    ``verify_access_token_principal`` at the API request boundary.
    """

    def __init__(self, credentials, refresh_request):
        if credentials is None or refresh_request is None:
            raise IdentityVerificationError(
                "explicit Google credentials and refresh request are required"
            )
        self._credentials = credentials
        self._refresh_request = refresh_request

    def access_token(self) -> str:
        try:
            if not self._credentials.valid:
                self._credentials.refresh(self._refresh_request)
            token = self._credentials.token
        except Exception:
            raise IdentityVerificationError(
                "explicit Google credentials could not provide an access token"
            ) from None
        if (
            not isinstance(token, str)
            or not token
            or token.strip() != token
            or "\n" in token
            or "\r" in token
        ):
            raise IdentityVerificationError(
                "explicit Google credentials returned an invalid access token"
            )
        return token


class CloudRunServiceAccountTokenSource:
    """Use only the Cloud Run metadata identity for API and proof tokens.

    The access credential and ID-token credential must both be created for
    the Cloud Run default service identity. The ID token is verified for a
    fixed audience on every access-token request, and its principal must match
    the configured service-account email and immutable numeric ID.
    """

    def __init__(
        self,
        *,
        request,
        access_credentials,
        identity_credentials,
        verify_identity_token,
        expected_email: str,
        expected_subject: str,
        audience: str,
    ):
        if (
            request is None
            or access_credentials is None
            or identity_credentials is None
        ):
            raise IdentityVerificationError("explicit Cloud Run credentials are required")
        if not callable(verify_identity_token):
            raise IdentityVerificationError("a Google ID-token verifier is required")
        if (
            not isinstance(expected_email, str)
            or PRINCIPAL_EMAIL.fullmatch(expected_email) is None
        ):
            raise IdentityVerificationError("expected service-account email is malformed")
        if (
            not isinstance(expected_subject, str)
            or re.fullmatch(r"[0-9]{1,30}", expected_subject) is None
        ):
            raise IdentityVerificationError("expected service-account ID is malformed")
        if (
            not isinstance(audience, str)
            or not audience
            or audience.strip() != audience
        ):
            raise IdentityVerificationError("a fixed identity-token audience is required")
        self._request = request
        self._access_credentials = access_credentials
        self._identity_credentials = identity_credentials
        self._verify_identity_token = verify_identity_token
        self._expected_email = expected_email
        self._expected_subject = expected_subject
        self._audience = audience
        self._last_token_digest = None
        self._last_principal = None

    @classmethod
    def from_metadata_server(
        cls,
        *,
        request,
        expected_email: str,
        expected_subject: str,
        audience: str,
    ):
        """Construct explicit metadata-server credentials without ADC lookup."""
        try:
            from google.auth.compute_engine import Credentials, IDTokenCredentials
            from google.oauth2 import id_token
        except ImportError as exc:
            raise IdentityVerificationError(
                "google-auth is required for Cloud Run identity"
            ) from exc
        access_credentials = Credentials(
            service_account_email="default",
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        identity_credentials = IDTokenCredentials(
            request=request,
            target_audience=audience,
            use_metadata_identity_endpoint=True,
        )

        def verify(token, active_request, expected_audience):
            return id_token.verify_token(
                token, active_request, audience=expected_audience
            )

        return cls(
            request=request,
            access_credentials=access_credentials,
            identity_credentials=identity_credentials,
            verify_identity_token=verify,
            expected_email=expected_email,
            expected_subject=expected_subject,
            audience=audience,
        )

    def access_token(self) -> str:
        try:
            self._access_credentials.refresh(self._request)
            access_token = self._access_credentials.token
            access_email = self._access_credentials.service_account_email
            self._identity_credentials.refresh(self._request)
            identity_token = self._identity_credentials.token
            claims = self._verify_identity_token(
                identity_token, self._request, self._audience
            )
        except Exception:
            raise IdentityVerificationError(
                "Cloud Run service identity could not be verified"
            ) from None
        if (
            not isinstance(access_token, str)
            or not access_token
            or access_token.strip() != access_token
        ):
            raise IdentityVerificationError("Cloud Run returned an invalid API access token")
        if access_email != self._expected_email:
            raise IdentityVerificationError("Cloud Run API credential has the wrong service account")
        if not isinstance(claims, dict):
            raise IdentityVerificationError("Cloud Run identity token claims are malformed")
        if claims.get("iss") != "https://accounts.google.com":
            raise IdentityVerificationError("Cloud Run identity token issuer is invalid")
        token_audience = claims.get("aud")
        if token_audience != self._audience:
            raise IdentityVerificationError("Cloud Run identity token audience is invalid")
        if (
            claims.get("email") != self._expected_email
            or claims.get("email_verified") is not True
        ):
            raise IdentityVerificationError(
                "Cloud Run identity email does not match the configured service account"
            )
        subject = claims.get("sub")
        if (
            subject != self._expected_subject
            or claims.get("azp") != self._expected_subject
        ):
            raise IdentityVerificationError(
                "Cloud Run identity subject does not match the configured service account"
            )
        expiry = claims.get("exp")
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, (int, float))
            or expiry <= time.time()
        ):
            raise IdentityVerificationError("Cloud Run identity token is expired or has no expiry")
        principal = VerifiedGoogleUser(
            auditor_id=f"google-service-account-{subject}", email=self._expected_email
        )
        self._last_token_digest = hashlib.sha256(access_token.encode()).digest()
        self._last_principal = principal
        return access_token

    def verify_principal(self, access_token: str) -> "VerifiedGoogleUser":
        """Return proof only for the exact API token paired with the ID token."""
        if not isinstance(access_token, str) or self._last_token_digest is None:
            raise IdentityVerificationError("Cloud Run access token has no identity proof")
        token_digest = hashlib.sha256(access_token.encode()).digest()
        if not hmac.compare_digest(token_digest, self._last_token_digest):
            raise IdentityVerificationError(
                "Cloud Run API token does not match its identity proof"
            )
        return self._last_principal


USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
PRINCIPAL_EMAIL = re.compile(
    r"^[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"
)
SUBJECT = re.compile(r"^[A-Za-z0-9._~-]{1,255}$")


@dataclass(frozen=True)
class VerifiedGoogleUser:
    auditor_id: str
    email: str


def verify_access_token_principal(
    transport: IdentityTransport,
    access_token: str,
    *,
    expected_email: str,
) -> VerifiedGoogleUser:
    """Return Google's stable user subject and verified email or refuse.

    The access token is sent only in the Authorization header to the fixed
    Google HTTPS UserInfo endpoint. Redirects are disabled and transport
    exceptions are suppressed so the credential cannot appear in error text.
    """
    if (
        not isinstance(access_token, str)
        or not access_token
        or access_token.strip() != access_token
        or "\n" in access_token
        or "\r" in access_token
    ):
        raise IdentityVerificationError("an explicit non-empty access token is required")
    if (not isinstance(expected_email, str)
            or PRINCIPAL_EMAIL.fullmatch(expected_email) is None):
        raise IdentityVerificationError("expected auditor email is malformed")
    parsed = urlsplit(USERINFO_URL)
    if parsed.scheme != "https" or parsed.hostname != "openidconnect.googleapis.com":
        raise IdentityVerificationError("Google UserInfo endpoint is invalid")
    try:
        response = transport.get(
            USERINFO_URL,
            params={},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=30,
            allow_redirects=False,
        )
    except Exception:
        raise IdentityVerificationError("Google UserInfo request failed") from None
    if response.status_code != 200:
        raise IdentityVerificationError(
            f"Google UserInfo request returned HTTP {response.status_code}"
        )
    try:
        body = response.json()
    except Exception:
        raise IdentityVerificationError("Google UserInfo response was not JSON") from None
    if not isinstance(body, dict):
        raise IdentityVerificationError("Google UserInfo response must be an object")
    email = body.get("email")
    if (not isinstance(email, str)
            or PRINCIPAL_EMAIL.fullmatch(email) is None
            or email != expected_email):
        raise IdentityVerificationError("access token email does not match expected auditor")
    if body.get("email_verified") is not True:
        raise IdentityVerificationError("access token principal email is not verified")
    subject = body.get("sub")
    if not isinstance(subject, str) or SUBJECT.fullmatch(subject) is None:
        raise IdentityVerificationError("Google UserInfo subject is missing or malformed")
    return VerifiedGoogleUser(auditor_id=f"google-user-{subject}", email=email)


def verified_google_credentials(
    token_source: AccessTokenSource,
    identity_transport: IdentityTransport | None,
    *,
    expected_email: str | None,
    expected_auditor_id: str | None,
):
    """Create Google auth credentials that verify identity before every request.

    Google client libraries call ``before_request`` on credentials immediately
    before sending each HTTP request. Refreshing from the injected source and
    verifying UserInfo here binds a storage API request to the same configured
    principal even when a token source rotates tokens between calls.
    """
    source_verifier = getattr(token_source, "verify_principal", None)
    if callable(source_verifier):
        if expected_email is not None and (
            not isinstance(expected_email, str)
            or PRINCIPAL_EMAIL.fullmatch(expected_email) is None
        ):
            raise IdentityVerificationError("expected Google principal email is malformed")
        if expected_auditor_id is not None and not isinstance(expected_auditor_id, str):
            raise IdentityVerificationError("expected Google principal ID is malformed")
    else:
        if identity_transport is None:
            raise IdentityVerificationError("Google user identity transport is required")
        if (
            not isinstance(expected_email, str)
            or PRINCIPAL_EMAIL.fullmatch(expected_email) is None
        ):
            raise IdentityVerificationError("expected Google user email is malformed")
        if (
            not isinstance(expected_auditor_id, str)
            or re.fullmatch(r"google-user-[A-Za-z0-9._~-]{1,255}", expected_auditor_id)
            is None
        ):
            raise IdentityVerificationError("expected Google user subject is malformed")
    try:
        from google.auth.credentials import Credentials
    except ImportError as exc:
        raise IdentityVerificationError("google-auth is required for GCP API access") from exc

    class PrincipalBoundCredentials(Credentials):
        def __init__(self):
            super().__init__()
            self.token = None
            self.expiry = None

        def refresh(self, request):
            try:
                token = token_source.access_token()
            except Exception:
                raise IdentityVerificationError(
                    "short-lived access token is unavailable"
                ) from None
            try:
                principal = source_verifier(token) if callable(source_verifier) else (
                    verify_access_token_principal(
                        identity_transport, token, expected_email=expected_email
                    )
                )
            except Exception:
                raise IdentityVerificationError("Google token principal could not be verified") from None
            if expected_email is not None and principal.email != expected_email:
                raise IdentityVerificationError("access token email does not match expected principal")
            if expected_auditor_id is not None and principal.auditor_id != expected_auditor_id:
                raise IdentityVerificationError("access token subject does not match expected principal")
            self.token = token
            # Google auth treats expiry as advisory. The adapter calls refresh
            # on every request, so this value is not used to extend trust.
            self.expiry = datetime.now(timezone.utc) + timedelta(seconds=1)
            self._verified_principal = principal

        def before_request(self, request, method, url, headers):
            self.refresh(request)
            self.apply(headers)

    return PrincipalBoundCredentials()
