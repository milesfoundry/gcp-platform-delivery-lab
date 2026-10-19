"""Verify exact GCP recovery controls and atomically prepare the registered run.

This command performs read-only Google API checks, then appends one generation-
conditional control-verification receipt to the exact run ledger object. It
does not run Terraform, create the run project, or publish container images.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

from gcp_cleanup_adapter import UrllibTransport
from gcp_recovery_control_verifier import GCPRecoveryControlVerifier
from gcp_token_identity import (
    ExplicitGoogleCredentialTokenSource,
    IdentityVerificationError,
    verify_access_token_principal,
)
from register_gcp_run import _RequestsIdentityTransport, _build_ledger, _load_intent
from run_contract import validate_intent


SCOPES = ["https://www.googleapis.com/auth/cloud-platform", "openid", "email"]


def _build_token_source(credentials_file: Path):
    try:
        with credentials_file.open(encoding="utf-8") as source:
            document = json.load(source)
        if not isinstance(document, dict) or document.get("type") != "authorized_user":
            raise ValueError("credential file must contain an authorized-user credential")
        import requests
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        credentials = Credentials.from_authorized_user_file(str(credentials_file), scopes=SCOPES)
        return ExplicitGoogleCredentialTokenSource(credentials, Request()), requests.Session()
    except ValueError:
        raise
    except Exception:
        raise RuntimeError("explicit Google credentials could not be loaded") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent", required=True, type=Path)
    parser.add_argument("--credentials-file", required=True, type=Path)
    parser.add_argument("--ledger-bucket", required=True)
    parser.add_argument("--ledger-project-id", required=True)
    parser.add_argument("--expected-email", required=True)
    parser.add_argument("--expected-auditor-id", required=True)
    args = parser.parse_args(argv)

    lease = None
    ledger = None
    try:
        manifest = validate_intent(
            _load_intent(args.intent), now=datetime.now(timezone.utc)
        )
        recovery_project = manifest["scope"]["recovery_project_id"]
        ledger_bucket = manifest["scope"]["ledger_bucket_name"]
        if args.ledger_project_id != recovery_project:
            raise ValueError("ledger project must match the recovery project in the intent")
        if args.ledger_bucket != ledger_bucket:
            raise ValueError("ledger bucket must match the run intent")

        token_source, identity_session = _build_token_source(args.credentials_file)
        token = token_source.access_token()
        principal = verify_access_token_principal(
            _RequestsIdentityTransport(identity_session), token,
            expected_email=args.expected_email,
        )
        if principal.auditor_id != args.expected_auditor_id:
            raise IdentityVerificationError("Google user subject does not match the expected operator")

        verifier = GCPRecoveryControlVerifier(UrllibTransport(), token_source, manifest)
        before = _build_ledger(
            credentials_file=args.credentials_file,
            ledger_bucket=ledger_bucket,
            ledger_project_id=recovery_project,
            expected_email=args.expected_email,
            expected_auditor_id=args.expected_auditor_id,
        )
        ledger = before
        current = ledger.read(manifest["run_id"])
        if current.manifest != manifest or current.manifest["state"] != "control_bootstrapping":
            raise ValueError("ledger manifest must exactly match the initial control-bootstrap intent")
        observations = verifier.verify()
        after_verification = ledger.read(manifest["run_id"])
        if after_verification.generation != current.generation:
            raise RuntimeError("ledger changed during recovery-control verification; retry from the new generation")

        lease = ledger.acquire_lease(
            manifest["run_id"], expected_generation=current.generation,
            owner="operator:control-bootstrap", ttl=timedelta(minutes=5),
        )
        prepared = ledger.record_recovery_control_verification(
            manifest["run_id"], expected_generation=lease.generation,
            lease_owner="operator:control-bootstrap", observations=observations,
        )
        released = ledger.release_lease(
            manifest["run_id"], expected_generation=prepared.generation,
            owner="operator:control-bootstrap",
        )
        receipt = released.manifest["recovery_control_receipts"][-1]
        json.dump({
            "run_id": released.run_id,
            "state": released.manifest["state"],
            "generation": released.generation,
            "verified_controls": len(observations),
            "receipt_id": receipt["receipt_id"],
        }, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        if lease is not None and ledger is not None:
            try:
                latest = ledger.read(lease.run_id)
                if latest.metadata["lease"]["owner"] == "operator:control-bootstrap":
                    ledger.release_lease(
                        lease.run_id, expected_generation=latest.generation,
                        owner="operator:control-bootstrap",
                    )
            except Exception:
                pass
        message = str(exc) if isinstance(exc, ValueError) else "verification failed closed"
        print(f"recovery-control verification rejected: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
