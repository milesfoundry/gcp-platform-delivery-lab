"""Register an exact GCP run intent in the durable ledger before provisioning.

This command is intentionally limited to one create-only ledger write. It does
not discover ambient credentials, create recovery controls, create projects, or
run Terraform. Repeating the command with the same intent reconciles a lost
ledger-create response; a different intent for the same run ID is rejected.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from gcp_token_identity import ExplicitGoogleCredentialTokenSource
from run_ledger import GcsObjectStore, RunLedger, object_name
from run_contract import validate_intent


SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "openid",
    "email",
]


class _RequestsIdentityTransport:
    """Small identity transport that preserves the no-redirect verification."""

    def __init__(self, session):
        self._session = session

    def get(
        self, url, *, params, headers, timeout, allow_redirects
    ):
        return self._session.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )


def _load_intent(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("intent file cannot be read as JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("intent document must be a JSON object")
    return value


def _build_ledger(
    *,
    credentials_file: Path,
    ledger_bucket: str,
    ledger_project_id: str,
    expected_email: str,
    expected_auditor_id: str,
) -> RunLedger:
    """Build GCS access only from the explicitly named human credential file."""
    try:
        with credentials_file.open(encoding="utf-8") as source:
            credential_document = json.load(source)
        if (
            not isinstance(credential_document, dict)
            or credential_document.get("type") != "authorized_user"
        ):
            raise ValueError("credential file must contain an authorized-user credential")

        import requests
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        credentials = Credentials.from_authorized_user_file(
            str(credentials_file), scopes=SCOPES
        )
        token_source = ExplicitGoogleCredentialTokenSource(
            credentials, Request()
        )
        identity_transport = _RequestsIdentityTransport(requests.Session())
        store = GcsObjectStore(
            ledger_bucket,
            project_id=ledger_project_id,
            token_source=token_source,
            identity_transport=identity_transport,
            expected_email=expected_email,
            expected_auditor_id=expected_auditor_id,
        )
    except ValueError:
        raise
    except Exception:
        # SDK exceptions can include request details. Keep CLI output free of
        # credential material and provider response bodies.
        raise RuntimeError("explicit Google credentials or ledger setup failed") from None
    return RunLedger(store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent", required=True, type=Path)
    parser.add_argument("--credentials-file", required=True, type=Path)
    parser.add_argument("--ledger-bucket", required=True)
    parser.add_argument("--ledger-project-id", required=True)
    parser.add_argument("--expected-email", required=True)
    parser.add_argument("--expected-auditor-id", required=True)
    args = parser.parse_args(argv)

    try:
        intent = _load_intent(args.intent)
        intent = validate_intent(intent, now=datetime.now(timezone.utc))
        run_id = intent.get("run_id")
        name = object_name(run_id)
        if intent.get("scope", {}).get("recovery_project_id") != args.ledger_project_id:
            raise ValueError("ledger project must match the recovery project in the intent")
        if intent["scope"]["ledger_bucket_name"] != args.ledger_bucket:
            raise ValueError("ledger bucket must match the run intent")
        ledger = _build_ledger(
            credentials_file=args.credentials_file,
            ledger_bucket=args.ledger_bucket,
            ledger_project_id=args.ledger_project_id,
            expected_email=args.expected_email,
            expected_auditor_id=args.expected_auditor_id,
        )
        snapshot = ledger.create(intent)
        result = {
            "registered": True,
            "run_id": snapshot.run_id,
            "state": snapshot.manifest["state"],
            "ledger_object": name,
            "intent_sha256": snapshot.metadata["intent_sha256"],
            "generation": snapshot.generation,
            "deadline_at": snapshot.manifest["deadline_at"],
        }
        json.dump(result, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else "registration failed"
        print(f"run registration rejected: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
