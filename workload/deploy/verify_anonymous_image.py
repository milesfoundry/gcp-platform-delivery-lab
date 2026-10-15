"""Boundedly verify public runtime image content without account credentials."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


class PublicRedirect(HTTPRedirectHandler):
    """Do not forward a registry bearer token to blob-storage redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header("Authorization")
        return redirected


def verify(image: str) -> dict:
    if not re.fullmatch(
            r"ghcr\.io/milesfoundry/report-workshop@sha256:[0-9a-f]{64}", image):
        raise ValueError("supply an exact published workload index")
    opener = build_opener(PublicRedirect())
    with urlopen("https://ghcr.io/token?service=ghcr.io&scope="
                 "repository:milesfoundry/report-workshop:pull", timeout=20) as response:
        token = json.load(response)["token"]
    total = 0
    verified = []

    def fetch(kind: str, digest: str) -> bytes:
        nonlocal total
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("invalid registry content digest")
        request = Request("https://ghcr.io/v2/milesfoundry/report-workshop/" +
                          kind + "/" + digest, headers={
                              "Authorization": "Bearer " + token,
                              "Accept": "application/vnd.oci.image.index.v1+json, "
                                        "application/vnd.oci.image.manifest.v1+json"})
        with opener.open(request, timeout=30) as response:
            raw = response.read(32 * 1024 * 1024 + 1)
        total += len(raw)
        if len(raw) > 32 * 1024 * 1024 or total > 64 * 1024 * 1024:
            raise ValueError("bounded registry verification exceeded")
        if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("registry digest mismatch")
        verified.append(digest)
        return raw

    index = json.loads(fetch("manifests", image.split("@", 1)[1]))
    platforms = []
    for entry in index["manifests"]:
        if entry["platform"]["os"] != "linux":
            continue
        platforms.append(entry["platform"]["architecture"])
        manifest = json.loads(fetch("manifests", entry["digest"]))
        for blob in [manifest["config"]] + manifest["layers"]:
            if blob["digest"] not in verified:
                fetch("blobs", blob["digest"])
    if sorted(platforms) != ["amd64", "arm64"]:
        raise ValueError("expected exactly both runtime architectures")
    return {"image": image, "verified_at": datetime.now(timezone.utc).isoformat(),
            "verification": "anonymous index, platform manifests, configs and all "
                            "runtime layers downloaded and SHA256 checked; no account credential",
            "platforms": sorted(platforms), "verified_digests": verified,
            "download_bytes": total, "anonymous_pull_verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite an observation")
    record = verify(args.image)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    print("passed: anonymous AMD64/ARM64 runtime content verified")


if __name__ == "__main__":
    main()
