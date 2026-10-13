# Image release and hosted HA qualification

## Explicit deployment identity release — 2026-10-07

Source `2202e13962914f71e56b146ff7632282b9d04492` passed Validate
[37687488181](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37687488181)
and Publish and qualify HA
[37687979581](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37687979581).
The immutable AMD64/ARM64 index is
`ghcr.io/talisman36935/report-workshop@sha256:3c08b2754404fdc14dda81ee4e0bc9f9f640a3223ccd89988f19e74250c41816`.
Native ARM migration/API/worker golden smoke passed. Hosted kind recorded primary
promotion in 59.675 seconds, then workload-node loss, database promotion and
surviving-application recovery in 86.69 seconds. Twenty accepted jobs were pending
at node loss; two API and two worker replicas survived, accepted reports remained
preserved, the test completion gate was removed, and the named cluster was deleted.
An independent anonymous verifier SHA256-checked the index, both platform manifests,
configs and runtime layers (28,921,673 bytes).

The exact sanitized publication, anonymous-pull and hosted-kind records are linked
from [the source-scoped qualification observation](observations/2202e13/qualification.md).
The publication record preserves the state at publish time; subsequent checks are
separate evidence. This is a one-host, three-simulated-worker experiment: no cloud
was provisioned, no physical/cloud zone failure was tested, and this is not AWS/EKS
qualification or signature verification.

## Poll-based readiness release — 2026-10-06

Source `3b1abf3b791b4ab95f852c860786ca91d8c0a381` passed hosted Validate
[37517856995](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37517856995)
and exact-source publish/qualification
[37518413015](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37518413015).
The immutable index is
`ghcr.io/talisman36935/report-workshop@sha256:dfba95425f82b619987f307e63e2e2720f9395a5a2c7836ec2d2a25f7d398d23`.
Native ARM migration, API, worker readiness and golden-report smoke passed.
Kubernetes primary promotion took 59.859 seconds; simulated worker-node loss,
database promotion and surviving application readiness took 92.901 seconds.
Two API and two worker replicas survived. Twenty accepted jobs were outstanding
at the fault; baseline, queued and recovery reports were preserved. The completion
gate was removed and the named cluster was deleted.

The anonymous AMD64/ARM64 verifier downloaded and SHA256-checked the index,
platform manifests, configs and all runtime layers without credentials
([28,946,103 bytes](observations/3b1abf3/anonymous-pull.json)). The hosted
qualification and independent pull evidence are summarized in
[observations/3b1abf3/qualification.md](observations/3b1abf3/qualification.md).
Earlier failures are preserved there too; they are not overwritten or counted as
passes. This remains a one-host kind simulation, not cloud-zone/CSI/IAM evidence.

## Workload-identity-separated release — 2026-10-06

Source `32c98ffd9cacb528c2d017f7ea39b54536ca2211` passed GCP Validate
[37493189725](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37493189725)
and Publish and qualify HA
[37493759179](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37493759179)
for `ghcr.io/talisman36935/report-workshop@sha256:47c7464df5cb1d20ba05eeb391d211309919c477503316d7c8fcf6d4feb43fe5`.
Native ARM application smoke, Kubernetes HA and anonymous AMD64/ARM64 image
content verification passed. The
[HA observation](observations/32c98ff/kubernetes-ha.json) records database primary
promotion in 60.238 seconds and primary-worker loss recovery in 79.268 seconds;
twenty accepted jobs were outstanding at node loss, two API/worker replicas
survived, golden reports were preserved, full readiness returned and the cluster
was deleted. The
[anonymous-pull record](observations/32c98ff/anonymous-pull.json) records SHA256
verification of the index, both platform manifests/configs and runtime layers
(28,915,114 bytes).

This is Kubernetes/PostgreSQL process behavior on one hosted runner with three
labelled simulated workers. No cloud was provisioned and no physical zone failure,
cloud CSI, IAM or cloud telemetry was tested. The source/image pair is consumed by
the companion AWS fixture; hosted application GitOps against it passed
[37496120127](https://github.com/milesfoundry/aws-kubernetes-reconciliation-lab/actions/runs/37496120127),
but this remains local kind/Flux evidence, not AWS/EKS qualification.

## Current adapter-capable release — 2026-10-06

Source `0e0a6133a18e4cfb10aae4defabd5b8f52ec69b5` passed full Validate
[37398071004](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37398071004).
[Release 37398470462](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37398470462)
passed publication, native ARM migration/API/worker golden smoke and hosted
Kubernetes HA for
`ghcr.io/talisman36935/report-workshop@sha256:505f63c5428ef76ccbd39389b623962f8abc3bc72e73bc73b1b5cfcf70bcc23a`.
The [release record](observations/0e0a613/image-release.json) declares schema-check
and cloud-queue-object-v1 capabilities. The image includes linked dependency
licenses/notices. [Anonymous verification](observations/0e0a613/anonymous-pull.json)
downloaded and SHA256-checked both platform manifests, configs and all runtime
layers (28,915,115 bytes) without account credentials.

The [runtime observation](observations/0e0a613/kubernetes-ha.json) records primary
promotion in 59.793 seconds and primary-worker loss recovery in 83.762 seconds,
including the respective readiness/access checks. Twenty jobs were outstanding
at the node fault; all 31 baseline/queued/recovery reports remained golden. Two
API/worker replicas survived, full readiness returned, the test completion gate
was removed and cluster deletion passed. New schema-check init gates were used.
These are simulated-zone process tests on one hosted machine, not cloud HA or
live cloud-adapter/GitOps qualification. The earlier release below remains history.

## Publishing contract and earlier qualified release

The manually dispatched Publish and qualify HA workflow accepts only a reviewed
main SHA with a successful Validate push run. It builds the source into a small
non-root multi-command image for AMD64 and ARM64 using native cross-compilation,
publishes to GHCR, then records the index/platform digests and build attestations.
The sha tag is a convenience; consumers must pin the immutable digest.

The workflow uses short-lived repository-scoped GitHub tokens. Ordinary/fork
validation does not publish packages or receive package-write permission.
Build provenance/SBOM generation is not a verified signature claim.
The ARM job runs migration, API, worker and golden assertions on a native ARM host.

GHCR initially creates a private package. Until the owner changes its visibility
and anonymous pulls are tested, release records explicitly do not claim public
pull access. Hosted qualification uses a temporary scoped image-pull secret,
passed in memory and removed with the cluster; no token or DB secret enters artifacts.

The release for source `6a6e522b0f67925e2e5fab476c61350582aadf1e` is publicly
pullable at
`ghcr.io/talisman36935/report-workshop@sha256:ff32688b7c8f2764ea46a32e8813d8e45585615d494c116324dfc5f2b7307d9f`.
Native ARM migration/API/worker golden smoke passed in
[37392244169](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37392244169).
The separate [anonymous-pull observation](observations/6a6e522/anonymous-pull.json)
verifies both manifests, configs and every runtime layer by digest without an
account credential. The original release artifact records what was known at
publication time; it is not rewritten to backdate later checks.

Recheck public content with bounded downloads (32 MiB per blob, 64 MiB total):

```sh
python3 workload/deploy/verify_anonymous_image.py \
  --image ghcr.io/talisman36935/report-workshop@sha256:505f63c5428ef76ccbd39389b623962f8abc3bc72e73bc73b1b5cfcf70bcc23a \
  --output output/anonymous-pull.json
```

The verifier never uses local Docker/account credentials and drops registry
authorization on blob-storage redirects. This proves pullable runtime content,
not signature authenticity or Kubernetes/cloud runtime behavior.

## Hosted Kubernetes experiment

[Run 37392244169](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37392244169)
passed all three jobs. The [runtime observation](observations/6a6e522/kubernetes-ha.json)
records primary `report-db-1` to `report-db-2` in 59.726 seconds (including full
database readiness), then primary-worker loss and promotion back to `report-db-1`
in 88.477 seconds (including surviving-app access). Two API/worker replicas
survived. All twenty queued jobs were outstanding at fault time; five baseline,
twenty queued and six recovery reports matched the golden output. The bounded
completion trigger was removed, full readiness restored and cluster deletion
confirmed. These are this experiment's observation intervals, not production SLOs.

Earlier [release audit](observations/0a387dd/release-qualification.md) and
[failed runtime observation](observations/6db97f0/kubernetes-ha.json) remain
preserved. Operator installation uses server-side apply for large CRDs. The
pod-deletion harness needed an explicit grace instead of timing out against
the operator's default 1,800-second shutdown allowance.

The kind profile has one control plane and three workers labelled as simulated
zones on ONE GitHub runner. This is real Kubernetes/PostgreSQL process behavior,
not independent physical zones, cloud networking/storage, or cloud HA qualification.
All image/operator/schema inputs are version/digest/checksum pinned.

Two leader-electing CloudNativePG operator replicas occupy distinct workers.
Three database instances use required zone anti-affinity, persistent local-path
volumes, ANY1 required synchronous durability and failover quorum. Migration must
complete before the three API/worker replica Deployments are applied.

The test observes actual placement, ready replicas, immutable image references,
synchronous_standby_names and successful reports. It deletes the primary pod under
an explicit thirty-second termination grace (without force deletion), requires
promotion to a different primary
and validates previously acknowledged reports and new work.

It installs a bounded, test-only ten-second completion trigger, then queues twenty
durably accepted jobs, observes outstanding work and abruptly
stops only the kind worker hosting the current primary. It requires another primary
promotion, two surviving API/worker replicas, recovery of accepted work and golden
reports. The trigger/function are removed on the promoted primary before recovery
assertions; this changes no application image. The worker container is restarted
and full three-instance/replica readiness
must return. The recorded recovery interval includes control-plane detection and
operator observation; it is not a zero-downtime or universal zero-data-loss promise.

Local host-path PVCs are not zonal cloud disks. This experiment does not establish
CSI failover, partition safety, backup/restore, GCP/EKS IAM or provider cleanup.
The cleanup owner restores any stopped worker, deletes only the named disposable
cluster and removes its temporary kubeconfig. Failed cleanup cannot produce pass.
Artifacts contain allowlisted summaries, never raw secrets or database contents.

Before cloud execution: qualify CSI/storage deletion, network policies and workload
identity, image access, capacity during a node loss, full costs and spending protections.
Budget/lifetime settings remain an explicit approval gate. This workflow provisions
no GCP/AWS resources and is not automatically run on documentation changes.
