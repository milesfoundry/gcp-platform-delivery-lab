"""Explicit, confirmation-bound launcher for one registered GCP run.

The command can create one exact project and link billing only after reading a
registered run from its durable ledger. It uses a caller-named authorized-user
credential file and never discovers ADC or other credentials.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

from gcp_cleanup_adapter import UrllibTransport
from gcp_finance_preflight import GCPFinancePreflight
from gcp_project_bootstrap import GCPProjectBootstrapper
from gcp_project_lifecycle_adapter import GCPProjectLifecycleAdapter
from gcp_token_identity import ExplicitGoogleCredentialTokenSource
from run_ledger import GcsObjectStore, RunLedger


SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "openid",
    "email",
]
RUN_ID = re.compile(r"^lab-[a-z0-9](?:[a-z0-9-]{0,17}[a-z0-9])?$")
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GOOGLE_USER = re.compile(r"^google-user-[A-Za-z0-9._~-]{1,255}$")


class _RequestsIdentityTransport:
    def __init__(self, session):
        self._session = session

    def get(self, url, *, params, headers, timeout, allow_redirects):
        return self._session.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--credentials-file", required=True, type=Path)
    parser.add_argument("--ledger-bucket", required=True)
    parser.add_argument("--ledger-project-id", required=True)
    parser.add_argument("--expected-email", required=True)
    parser.add_argument("--expected-google-user", required=True)
    parser.add_argument("--confirm-project-id", required=True)
    parser.add_argument("--confirm-intent-sha256", required=True)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="explicitly authorize the next bounded attempt after reviewing the failure",
    )
    return parser


def _load_token_source(credentials_file: Path):
    try:
        with credentials_file.open(encoding="utf-8") as source:
            document = json.load(source)
        if not isinstance(document, dict) or document.get("type") != "authorized_user":
            raise ValueError("credential file must be an authorized-user credential")
        import requests
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        credentials = Credentials.from_authorized_user_file(
            str(credentials_file), scopes=SCOPES
        )
        return (
            ExplicitGoogleCredentialTokenSource(credentials, Request()),
            _RequestsIdentityTransport(requests.Session()),
        )
    except ValueError:
        raise
    except Exception:
        raise RuntimeError("explicit Google credential setup failed") from None


def _build_runtime(args, snapshot, token_source, identity_transport):
    project_id = snapshot.manifest["scope"]["project_id"]
    provider = GCPProjectLifecycleAdapter(
        UrllibTransport(),
        token_source,
        project_id=project_id,
        run_id=snapshot.run_id,
        parent=snapshot.manifest["scope"]["parent"],
        billing_account_name=snapshot.manifest["finance"]["billing_account_id"],
        enabled_services=snapshot.manifest["scope"]["enabled_api_services"],
        identity_transport=identity_transport,
        project_reclaimer_email=args.expected_email,
        project_reclaimer_auditor_id=args.expected_google_user,
    )
    finance = GCPFinancePreflight(
        UrllibTransport(),
        token_source,
        identity_transport,
        expected_email=args.expected_email,
        expected_subject=args.expected_google_user,
    )
    return provider, finance


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if RUN_ID.fullmatch(args.run_id) is None:
        parser.error("--run-id must be a bounded lab-prefixed identifier")
    if PROJECT_ID.fullmatch(args.ledger_project_id) is None:
        parser.error("--ledger-project-id is malformed")
    if PROJECT_ID.fullmatch(args.confirm_project_id) is None:
        parser.error("--confirm-project-id is malformed")
    if SHA256.fullmatch(args.confirm_intent_sha256) is None:
        parser.error("--confirm-intent-sha256 must be a SHA-256 hex digest")
    if GOOGLE_USER.fullmatch(args.expected_google_user) is None:
        parser.error("--expected-google-user must be a stable Google user subject")

    try:
        # Construct the explicit GCS client only after argument validation.
        token_source, identity_transport = _load_token_source(args.credentials_file)
        store = GcsObjectStore(
            args.ledger_bucket,
            project_id=args.ledger_project_id,
            token_source=token_source,
            identity_transport=identity_transport,
            expected_email=args.expected_email,
            expected_auditor_id=args.expected_google_user,
        )
        ledger = RunLedger(store)
        snapshot = ledger.read(args.run_id)
        if (
            snapshot.manifest["scope"]["ledger_bucket_name"] != args.ledger_bucket
            or snapshot.manifest["scope"]["recovery_project_id"] != args.ledger_project_id
        ):
            raise ValueError("ledger location does not match the registered run")
        if snapshot.manifest["scope"]["project_id"] != args.confirm_project_id:
            raise ValueError("project confirmation does not match the registered run")
        if snapshot.metadata["intent_sha256"] != args.confirm_intent_sha256:
            raise ValueError("intent confirmation does not match the registered run")
        # Reuse the already constructed, explicit store and token source.
        provider, finance = _build_runtime(
            args, snapshot, token_source, identity_transport
        )
        result = GCPProjectBootstrapper(
            ledger, provider, finance
        ).run(
            args.run_id,
            confirm_project_id=args.confirm_project_id,
            confirm_intent_sha256=args.confirm_intent_sha256,
            retry_failed=args.retry_failed,
        )
        json.dump({
            "run_id": result.snapshot.run_id,
            "project_id": result.snapshot.manifest["scope"]["project_id"],
            "state": result.snapshot.manifest["state"],
            "status": result.status,
            "generation": result.snapshot.generation,
        }, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except Exception:
        # Provider and authentication exceptions may contain response details.
        print("run activation stopped; inspect the sanitized status and ledger", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
