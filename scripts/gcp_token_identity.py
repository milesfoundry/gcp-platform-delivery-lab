"""Fail-closed Google user principal verification through OpenID Connect.

Google access tokens are opaque. This helper calls the standard UserInfo
endpoint with the bearer token in the Authorization header and stores Google's
stable subject identifier as the auditor identity. It supports human Google
user tokens with OIDC email scope; other credentials are intentionally refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    if not isinstance(access_token, str) or not access_token.strip():
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
