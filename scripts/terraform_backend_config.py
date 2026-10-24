"""Build secret-free, exact Terraform GCS backend configuration files."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

from run_contract import BUCKET_NAME, PROJECT_ID, ContractError, validate_intent


class BackendConfigError(ValueError):
    """Backend configuration is incomplete or unsafe."""


def lab_backend_config(manifest: dict) -> dict[str, str]:
    """Return the exact GCS backend bucket/prefix for the manifest's lab state."""
    try:
        normalized = validate_intent(manifest, new_run=False)
    except (ContractError, TypeError, ValueError):
        raise BackendConfigError("run manifest is invalid") from None
    run_id = normalized["run_id"]
    state = normalized["terraform_state"]["backend"]
    prefix = f"runs/{run_id}/terraform"
    if state["object"] != f"{prefix}/default.tfstate":
        raise BackendConfigError("manifest state path does not match the Terraform default workspace")
    if state["project_id"] != normalized["scope"]["recovery_project_id"]:
        raise BackendConfigError("lab state bucket must belong to the recovery project")
    return {
        "bucket": state["bucket"],
        "prefix": prefix,
        "project": state["project_id"],
    }


def bootstrap_backend_config(bucket: str, project_id: str) -> dict[str, str]:
    """Return the fixed persistent backend location for bootstrap Terraform."""
    if not isinstance(bucket, str) or not BUCKET_NAME.fullmatch(bucket):
        raise BackendConfigError("bootstrap state bucket name is invalid")
    if bucket.startswith("goog") or "google" in bucket:
        raise BackendConfigError("bootstrap state bucket name is reserved")
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise BackendConfigError("bootstrap state project ID is invalid")
    return {"bucket": bucket, "prefix": "portfolio/bootstrap", "project": project_id}


def render_backend_config(config: dict[str, str]) -> str:
    """Render only non-secret GCS backend fields as valid HCL."""
    if set(config) != {"bucket", "prefix", "project"}:
        raise BackendConfigError("backend config must contain only bucket and prefix")
    bucket = config["bucket"]
    prefix = config["prefix"]
    project = config["project"]
    if not isinstance(bucket, str) or not BUCKET_NAME.fullmatch(bucket):
        raise BackendConfigError("backend bucket name is invalid")
    if (not isinstance(prefix, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9/-]{2,127}", prefix)
            or any(part in {"", ".", ".."} for part in prefix.split("/"))):
        raise BackendConfigError("backend prefix is invalid")
    if not isinstance(project, str) or not PROJECT_ID.fullmatch(project):
        raise BackendConfigError("backend project ID is invalid")
    return (
        f"bucket = {json.dumps(bucket)}\n"
        f"prefix = {json.dumps(prefix)}\n"
        f"project = {json.dumps(project)}\n"
    )


def write_backend_config(path: Path, config: dict[str, str]) -> None:
    """Create a mode-0600 config file without replacing an existing path."""
    content = render_backend_config(config).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise BackendConfigError("backend config output path already exists") from None
    except OSError:
        raise BackendConfigError("backend config output path could not be created") from None
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        raise BackendConfigError("backend config output could not be written") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="kind", required=True)
    lab = subparsers.add_parser("lab")
    lab.add_argument("--intent", required=True, type=Path)
    lab.add_argument("--output", required=True, type=Path)
    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--bucket", required=True)
    bootstrap.add_argument("--project-id", required=True)
    bootstrap.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.kind == "lab":
            try:
                with args.intent.open(encoding="utf-8") as source:
                    manifest = json.load(source)
            except Exception:
                raise BackendConfigError("run intent could not be read") from None
            config = lab_backend_config(manifest)
        else:
            config = bootstrap_backend_config(args.bucket, args.project_id)
        write_backend_config(args.output, config)
        json.dump({"kind": args.kind, "config_written": True}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except BackendConfigError as exc:
        parser.exit(2, f"backend config failed closed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
