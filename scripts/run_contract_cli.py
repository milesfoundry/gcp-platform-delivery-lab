"""Local, provider-free validator and dry-run planner for GCP run records."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from run_contract import (
    ContractError,
    cleanup_plan,
    control_cleanup_plan,
    validate_intent,
    validate_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("intent", "manifest", "cleanup", "controls"))
    parser.add_argument("document", type=Path)
    args = parser.parse_args(argv)

    try:
        with args.document.open(encoding="utf-8") as source:
            value = json.load(source)
        if args.mode == "intent":
            record = validate_intent(value, now=datetime.now(timezone.utc))
            result = {
                "valid": True,
                "run_id": record["run_id"],
                "state": record["state"],
                "deadline_at": record["deadline_at"],
            }
        elif args.mode == "manifest":
            record = validate_manifest(value)
            result = {
                "valid": True,
                "run_id": record["run_id"],
                "state": record["state"],
                "resource_count": len(record["resources"]),
            }
        else:
            record = validate_manifest(value)
            planned = cleanup_plan(record) if args.mode == "cleanup" else control_cleanup_plan(record)
            result = {
                "dry_run": True,
                "mode": args.mode,
                "run_id": record["run_id"],
                "operations": [
                    {"type": item["type"], "id": item["id"]}
                    for item in planned
                ],
            }
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        print(f"run contract rejected input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
