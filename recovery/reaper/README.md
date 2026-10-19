# GCP project reaper image

This image packages the source-tested final retirement entrypoint. It validates
the configured run and the durable manifest, waits until the expiry deadline
and passed workload audit, then unlinks billing and submits deletion for only
the exact declared project. It does not perform workload cleanup or the
independent workload/closure audits.

The Dockerfile pins the Python base image by its Docker Official Images
multi-platform manifest digest. Python dependencies are pinned with hashes in
`requirements.lock`; update the lock when `scripts/requirements-recovery.txt`
changes. The image runs as UID/GID 65532 and contains only the modules required
by this entrypoint. The repository-level `.dockerignore` restricts the build
context to those files.

The `Validate` workflow builds and smoke-checks the image for pull requests and
main-branch changes. The separate `Reaper image` workflow has a manual publish
path that requires an explicit `publish=true` input and a semantic version, and
runs only from `main`. It publishes `linux/amd64` with BuildKit provenance and
an SBOM, then records the resulting immutable digest in the workflow summary.
This workflow has not been triggered to publish an image.

Cloud Run can import public GHCR images directly. A private GHCR image needs an
Artifact Registry remote repository, which this plan has not provisioned.
Before using the GHCR image in Cloud Run, make the package public and verify
that the Terraform input references the recorded digest. See Google's
[Cloud Run container image guidance](https://docs.cloud.google.com/run/docs/deploying#supported-container-registries-and-images).
