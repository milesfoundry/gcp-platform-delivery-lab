# Initial image release qualification

Source: `0a387dd7bae483368fd97fed74b86b5db9ae376d`.
Validate [37389924643](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37389924643)
passed. Publish and qualify HA
[37390250920](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37390250920)
published the image and passed native ARM migration/API/worker golden smoke.

Index: `ghcr.io/talisman36935/report-workshop@sha256:35d81b3d3fee01c0d242c3788cda0a2d7b284914dd246bd21e7c660d72dfcc1f`.
AMD64 manifest: `sha256:3449136b41b0563572da0ef35b82d0e1abed4f8757f5b5502ec0dd72ab9070fc`.
ARM64 manifest: `sha256:a767d8ab1169ecb2c3c6b02192ef58483a86cca20edaab05873113f686a6a2d5`.
Two build attestation manifests were recorded; independent signature verification
and anonymous pull access were not established.

The Kubernetes job failed in **Qualify PostgreSQL failover and workload node loss**.
The sanitized observation records `CalledProcessError`, no baseline jobs, no
primary promotion, no node failure and `cluster_deleted: true`. The run lasted
from 2026-10-05T23:46:37.561201Z to 23:47:25.234464Z.
This is a failed qualification, not HA evidence.

Original captured stderr was omitted, preventing a definitive root-cause claim.
Commit `623dd00` adds failed-stage recording and safe pre-secret diagnostics.
Read-only inspection of the pinned operator release found Cluster CRD JSON
282,706 bytes and Pooler 360,922 bytes, both above the 262,144-byte annotation
limit. Client-side apply is therefore a likely failure mechanism; `6db97f0`
uses server-side apply for that public manifest and emits its safe errors.
Verification of this hypothesis is pending a new hosted qualification.
