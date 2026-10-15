"""Record verified index structure, not an assertion of public registry access."""

import argparse
import json
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    if (not re.fullmatch(r"ghcr.io/milesfoundry/report-workshop@sha256:[0-9a-f]{64}", args.image)
            or not re.fullmatch(r"[0-9a-f]{40}", args.revision)):
        parser.error("supply exact release inputs")
    raw = subprocess.run(["docker", "buildx", "imagetools", "inspect", args.image,
                          "--raw"], check=True, capture_output=True, text=True).stdout
    manifests = json.loads(raw)["manifests"]
    platforms = {m["platform"]["architecture"]: m["digest"] for m in manifests
                 if m.get("platform", {}).get("os") == "linux"}
    if set(platforms) != {"amd64", "arm64"}:
        raise ValueError("release must include exactly both supported architectures")
    attestations = [m for m in manifests if m.get("annotations", {}).get(
        "vnd.docker.reference.type") == "attestation-manifest"]
    if len(attestations) != 2:
        raise ValueError("per-platform build attestations absent")
    output = Path("output")
    output.mkdir(exist_ok=True)
    record = {"source_revision": args.revision, "image": args.image,
              "capabilities": json.loads(Path(__file__).with_name("capabilities.json").read_text())["capabilities"],
              "platform_manifests": platforms, "build_attestation_manifests":
              [m["digest"] for m in attestations], "signature_verified": False,
              "anonymous_pull_verified": False, "runtime_qualification": "pending"}
    with (output / "image-release.json").open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
