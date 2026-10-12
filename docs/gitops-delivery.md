# Opt-in workload delivery profiles

The shared delivery renderer splits platform, migration and application identities.
It consumes one immutable release record with explicit `schema-check` and cloud
adapter capabilities; older HA releases are rejected rather than silently using
commands they do not implement. Generated trees are **blocked candidates**, not
cloud deployments. Current Config Sync and Flux roots remain contract-only.

```sh
python3 workload/deploy/render_delivery.py \
  --release output/image-release.json --owner config-sync \
  --storage-class QUALIFIED_CSI_CLASS --output profiles/delivery
# AWS consumes the same renderer from its verified shared-source checkout:
# --owner flux --backend aws --settings NONSECRET_RESOURCE_HANDOFF.json
```

The output directory must not already exist. Review/commit the output and activate
its root source explicitly; this command performs no Kubernetes/cloud operation.
Flux paths deliberately assume `profiles/delivery/` in the destination repository.
Config Sync's RepoSync path uses that same destination. Neither controller follows
an arbitrary temporary render directory.

## Exclusive ownership and readiness

Platform owns namespace, quota, default-deny/DNS/database-egress policies, database
CR, app identity, reconciler Role/RoleBinding and source delegation. Quota reserves
headroom for three DB instances, six application replicas and migration; it is not
a tested cloud capacity guarantee. CNPG operator/CRDs must be separately installed
by the platform owner first, not by an application reconciler.

Application delegation permits only ConfigMaps, Services, Deployments, Jobs and
PDBs inside the one namespace. It does not permit Secrets, ServiceAccounts, RBAC,
PVCs, namespaces or cluster resources. The renderer creates separate
`report-api`, `report-worker` and `report-migrate` ServiceAccounts, with token
automount disabled by default. Cloud identity is platform-owned: the GCP service
account annotation is attached only to `report-worker`; AWS workers receive the
explicit projected audience-scoped token, scoped role ARN and disabled EC2 metadata
fallback. API and migration pods receive neither provider settings nor the worker's
token volume. GCP identity annotation is checked against the selected project.
Provider metadata behavior and IAM trust remain unqualified until cloud preflight;
in particular, the node identity must not become an unintended fallback.

Config Sync gives the app reconciler the migration Job and app objects together;
read-only `schema-check` init containers block app start until the additive Job
migration finishes. Reconciliation order alone is not treated as a migration gate.
Flux emits a root graph with `platform -> migrations -> apps`, `wait: true`, bounded
timeouts, an explicit DB ready-instance health expression and Deployment rollout
health for the app root. The Deployment expression requires the current observed
generation and all desired replicas updated, ready and available. This avoids Flux's
default Deployment reader needing ReplicaSet/Pod reads, so the delegated Role stays
narrow. Migration/app reconcilers use the delegated namespace identity; platform
reconciliation requires separately authorized bootstrap ownership. There is no
application cluster-admin grant. The DB expression guards `status.readyInstances`,
not top-level `has(status)`; Flux/CEL cannot use that macro on a top-level property.
A resource without status remains unready until the controller writes it, bounded by
the reconciliation timeout.
See the [Flux health-check contract](https://fluxcd.io/flux/components/kustomize/kustomizations/#health-check-expressions).

## Fail-closed activation boundaries

The emitted default-deny is intentional. Database peer/control-plane/DNS policies,
provider/CNI-specific API destinations, workload HTTPS/metadata and telemetry egress,
API ingress, actual CSI, controller health and workload identity require a reviewed
provider overlay. The generic profile does **not** substitute allow-all egress or
pretend a local render is runnable in either cloud. IAM and network qualification
must precede the source-root change.

The dormant profile does not enable an OTLP exporter by default. If a reviewed
provider overlay enables tracing, it must explicitly set
`DEPLOYMENT_ENVIRONMENT_NAME` to the actual non-local environment and provide a
unique `WORKSHOP_RUN_ID`; application startup rejects missing or malformed identity
when trace export is enabled. Never carry the local Compose identity defaults into
cloud observations.

Unit tests prove disjoint identities, immutable release inputs, no legacy capability
fallback, schema init gates, owner/backend matching and limited application RBAC.
Focused renderer tests also require distinct API/worker/migration identities and
reject cloud settings or the AWS token projection on API/migration pods. The AWS
repository pins this exact renderer source/image after GCP validation, native ARM
and hosted HA qualification; its identity-separated fixture and hosted application
GitOps run passed without cloud credentials or resources. The latest qualification
promoted the immutable GCP source and image, then exercised rollback and drift
repair. See the
[AWS workload lock](https://github.com/talisman36935/aws-kubernetes-reconciliation-lab/blob/main/workload/source.json)
and [latest hosted qualification record](https://github.com/talisman36935/aws-kubernetes-reconciliation-lab/blob/f269ab95a773a77979dcde2f5c5292a8c12be773/docs/observations/bed9efd/workload-gitops.json).
That observation is local kind/Flux evidence only and does not qualify live AWS
IAM, EKS/CAPA/ACK, physical-zone failures, cloud networking or storage.
Rendered manifests, IAM token projections and Flux readiness expressions are not
live Config Sync or cloud authorization/admission evidence. The companion AWS repo's
[hosted Flux app experiment](https://github.com/talisman36935/aws-kubernetes-reconciliation-lab/blob/f269ab95a773a77979dcde2f5c5292a8c12be773/docs/observations/bed9efd/workload-gitops.json)
passed immutable source/image promotion, rollback, drift repair, twelve golden
jobs, actual delegated-controller denial, local network allow/deny probes and
cleanup in [37522142251](https://github.com/talisman36935/aws-kubernetes-reconciliation-lab/actions/runs/37522142251).
Candidate source `3b1abf3` and its image passed GCP validation and native ARM/
Kubernetes HA qualification before AWS promotion. It derives the local fixture
from this pinned shared renderer, adds an explicit operator/DB/DNS/local API overlay
and uses the same Deployment CEL health contract.
The generic Flux renderer now emits that contract; its unit tests require the exact
expression and reject missing or weakened checks. The hosted fixture independently
qualifies it with the narrow Role. Default cloud roots remain unchanged/blocked, and
the generated profile still needs provider identity/network/storage qualification.
Config Sync runtime remains unqualified.

Ordinary rollback reverts the app digest/config; it does not reverse additive DB
migrations. Do not let RootSync and Flux or Cloud Deploy own the same identities.
Before cloud activation, verify budget-alert delivery to the approved private
recipient, a billing-currency threshold informed by the £5 planning reference,
current account usage and credit runway, maximum lifetime and
bootstrap/state teardown policy, and implement independent cleanup. Alerts are not
a hard monetary cap.
