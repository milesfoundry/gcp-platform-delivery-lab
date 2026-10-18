# GCP ephemeral-run safety and recovery design

Status: CP-1B credential-free safety implementation in review. It does not
authorize enabling APIs, applying Terraform, creating cloud resources, or spending.

## Decision

Use a provider-managed expiry reaper outside GitHub Actions and outside GKE,
backed by a private durable GCS run ledger. A per-run Cloud Scheduler entry
invokes a generic bootstrap-owned Cloud Run Job every five minutes; the job
cleans only the exact active run. The ledger is not Terraform state. Keep Terraform state
local and private as currently designed; preserve it through ordinary teardown
and independent audit. If the workflow runner is lost, the reaper acts from the
durable run record and provider APIs, then reports completion without depending
on local state.

This is the preferred design to implement, not a qualified service. It uses
Cloud Scheduler/Cloud Run and a private ledger object in the dedicated lab
project. That leaves a small persistent storage and scheduled-control-plane cost;
measure it, alert on it, and delete or explicitly retain it under the approved
account policy. The reaper must remain available through run cleanup and audit.
Delete the run-scoped Scheduler entry only after that audit; retain the generic
Cloud Run Job as bootstrap recovery infrastructure until explicit project close.
The GCS ledger bucket is project-level recovery infrastructure and is not deleted by run cleanup.
Document its exact owner, retention, backup and eventual project-close procedure.

The GCS ledger is independent of the runner and Kubernetes management plane, but
shares the GCP project and provider control plane. If project IAM, Cloud Scheduler,
Cloud Run or GCS is unavailable, the independent operator recovery path is the
existing privileged human workflow after access is restored. Do not claim recovery
from total project/control-plane loss. Cloud Asset Inventory is an additional
discovery source, not the sole cleanup proof: Google documents its current asset
data as eventually consistent and best effort ([asset types](https://docs.cloud.google.com/asset-inventory/docs/asset-types)).

## Ownership and resource coverage

All billable/run resources receive a unique lowercase `lab-…` run ID in provider
labels where supported. Resources without suitable labels use unique deterministic
names containing the run ID. Before creating any resource, the manifest records
its expected type, fully qualified ID/name, owner, dependency and discovery method.
Immediately after creation, record provider-returned identifiers. If the response
is lost before recording, recover by exact run label/name and provider-specific
list API; ambiguity or missing discovery stops cleanup as incomplete. Cloud Asset
Inventory supplements these checks because it can lag.

| Resource / current owner | Run identity and recovery lookup | Ordered cleanup and independent audit | Cost / retention |
| --- | --- | --- | --- |
| Lab API enablements — bootstrap Terraform root | Project ID plus explicit API service name; project-level services are shared controls, not run-tagged | Keep enabled during run cleanup and bootstrap state removal; disable only under a separate project-close plan after confirming no other project use | Usually control-plane configuration; retained project setting, verify current billing treatment |
| VPC — lab Terraform root | Add `lab_run_id` label if the provider/API supports it; otherwise unique VPC name; exact project/network self-link | Delete after subnet, router/NAT and cluster dependencies; verify exact VPC absent | Project resource, run lifetime |
| Subnet and secondary ranges — lab root | Run-specific name and VPC URI; confirm tag support | Delete after cluster and router/NAT; audit exact self-link | Project resource, run lifetime |
| Router and Cloud NAT — lab root | Run-specific names and router/subnetwork URIs; confirm tag support | Delete NAT, then router, after cluster; audit NAT config and router absence | Billable while present; include setup/deletion interval |
| GKE cluster and zonal node pool — lab root | Cluster resource labels include run ID; unique cluster/name; node pool tied to cluster; inventory exact cluster ID and all zones | Drain/stop application producers, export bounded evidence, remove run Config Sync membership/config in reviewed dependency order, then delete cluster normally with deletion protection explicitly disabled for teardown; audit cluster and every node instance absent | Main billable resource; deadline begins before first billable bootstrap resource |
| Fleet membership/config — lab root; Config Sync Feature — bootstrap root | Membership namespaced by run cluster ID; the project-wide `configmanagement` Feature is a shared singleton and is protected from destroy | Remove run feature membership/config before cluster/network; keep the singleton Feature under bootstrap ownership through all runs | Potential feature/service charge; explicitly measure and record |
| Node service account and project role — bootstrap root | One persistent `portfolio-lab-nodes` identity with only `roles/container.defaultNodeServiceAccount`; it is never in the run manifest | Retain across runs; reaper never mutates project IAM policy or deletes this identity | Persistent least-privilege project identity; no application cloud permissions |
| Future Pub/Sub topics/subscriptions and GCS report objects — CP-2 workload Terraform | Every topic/subscription/object carries run ID or lives under a dedicated run prefix; store exact full resource URI/object generation | Stop publishers, drain/delete subscriptions and queues; export evidence; delete only run-owned object generations after evidence verification; audit queues and versions absent | Include retained versions/egress/operations; evidence copies are separately allowlisted |
| Bootstrap WIF pool/provider, CI service account, IAM grants — bootstrap root | Explicit bootstrap owner record, not a run ID | Keep through active run and audit; remove only in reviewed bootstrap/project-close sequence after run inventory is empty | Retained exception until explicit removal; protect from janitor |
| Existing project billing budget and Monitoring channel/policies — bootstrap/account owner | Existing verified object IDs; import/reconcile before managing, never create competing objects blindly | Keep alerting through teardown and delayed billing review; remove only through explicit account closeout | Budget is a warning, not a cap; recipient config/state remain private |
| Private GCS run ledger and generic Cloud Run Job — bootstrap/recovery owner | One private ledger path per run; the reaper Job is a persistent shared control and never appears in a run inventory | Retain through all runs and project closeout; reaper role does not permit deleting itself | Ledger bucket and reaper execution cost are persistent; measure and alert |
| Cloud Scheduler entry — run recovery owner | Unique name containing the run ID; invoke the shared reaper Job with exact run/project scope | Preserve through workload cleanup and independent audit; delete the per-run schedule only after audit. A failed run retains/recreates its schedule during recovery | Scheduler charge while each run is active |

The exact provider permission set for the reaper is a CP-1B exit check. Derive
permissions from the API operations above and test denied operations. Do not use
project Owner/Editor or a broad project-admin role. Separate the GitHub federated
provisioning identity, GKE node identity, janitor execution identity and human
recovery identity. The reaper must not receive `resourcemanager.projects.setIamPolicy`:
Google authorizes that permission at project scope, so it could rewrite unrelated
project grants ([permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/resourcemanager)).
The node service account and its one GKE node role therefore live in bootstrap,
not in the run manifest. GKE cluster deletion and Compute network deletion have
distinct permissions and may support lower-level resource grants; validate their
resource-name condition support before selecting roles ([GKE permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/container),
[Compute resource access](https://docs.cloud.google.com/compute/docs/access/managing-access-to-resources)).
Use short-lived federation for Actions and an attached Cloud Run service identity
for the reaper. Google's federation guidance recommends
dedicated service accounts and limiting both trusted identities and accessible
resources ([WIF deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines),
[WIF best practices](https://docs.cloud.google.com/iam/docs/best-practices-for-using-workload-identity-federation)).

### IAM boundary still to qualify

The credential-free implementation currently protects against wrong run IDs and
unknown inventory types, but it does not prove Google IAM denies cross-run
deletion. The reaper role design remains open until every `get`, `list`, `delete`,
and IAM-condition permission is mapped to a lowest-level resource and negative
tests show a different run's network, cluster, membership, and shared bootstrap
objects cannot be changed. If the required permission is project-wide, stop and
revise the isolation model before CP-1C; exact IDs in application code are not an
IAM boundary.

#### Current permission-boundary finding (2026-10-18)

Official IAM references distinguish API authorization from where a role can be
bound and which resources expose condition attributes. GKE cluster deletion is
authorized against the requested cluster, and GKE clusters expose
`resource.name` for IAM Conditions. Compute subnetworks accept allow policies,
but Compute VPC networks and Cloud Routers are not listed as resources that
accept allow policies; the documented Compute `resource.name` list also does
not include VPC networks, subnetworks, routers or Cloud NAT configuration.
Consequently, the current single-project design cannot yet demonstrate an IAM
deny boundary for every network resource using a run-name condition. A reaper
grant at the lab-project level would be broader than the run manifest, even
when its application code submits exact resource IDs.

Do not implement a live reaper role or enable the scheduler under the current
single-project layout. CP-1A must compare and qualify one of these boundaries:

1. Put each run's billable resources in a disposable, run-dedicated project;
   keep the ledger, scheduler and generic reaper in a separate persistent
   recovery project. Grant cleanup permissions only on that run project, and
   keep `resourcemanager.projects.setIamPolicy` and project deletion outside
   the reaper role. Verify all APIs, Fleet/Config Sync operations, node
   identities, billing alerts and inventory/audit flows work across this
   boundary before changing Terraform.
2. Retain one lab project only if an exact conditional-role or deny-policy
   design is shown to cover every destructive and discovery permission for
   network, subnet, router/NAT, GKE, Fleet membership and any future workload
   resources, including negative cross-run tests. Labels and application-side
   ID checks alone do not satisfy this option.

The first option is the working design candidate, not an approved live change.
It adds project provisioning and teardown to the immutable run manifest and
requires updated cost, quota, budget-alert and failure-recovery analysis. The
existing Terraform roots and contract still assume one dedicated lab project;
they must not be treated as implementing per-run project isolation.

Project lifecycle is a separate authority boundary: project creation requires
`resourcemanager.projects.create` on the parent, and the Resource Manager create
method does not attach a billing account. Billing association needs its own
permission and verification step. Project deletion requires
`resourcemanager.projects.delete` on the parent, so automatic project deletion
is not part of the reaper candidate; define and qualify a separate post-audit
reclamation path or document the retained empty project and its quota/lifecycle
policy. For cost monitoring, the Budget API can scope an alerts-only budget to
one project under the billing account, while Google recommends keeping
programmatic budget-notification topics in a FinOps administration project.
These are design capabilities, not proof that the current account has the
required parent or billing permissions.

Evidence references: [resource types that accept allow policies](https://docs.cloud.google.com/iam/docs/resource-types-with-policies),
[IAM Condition resource attributes](https://docs.cloud.google.com/iam/docs/conditions-resource-attributes),
[GKE API permissions](https://docs.cloud.google.com/kubernetes-engine/docs/reference/api-permissions),
[Compute IAM roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/compute),
and [GKE IAM roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/container).
Project and billing references: [create projects](https://docs.cloud.google.com/resource-manager/docs/creating-managing-projects),
[project create API](https://docs.cloud.google.com/resource-manager/reference/rest/v1/projects/create),
[project IAM access control](https://docs.cloud.google.com/resource-manager/docs/access-control-proj),
[project-filtered alerts-only budgets](https://docs.cloud.google.com/billing/docs/how-to/budget-api),
and [programmatic budget notifications](https://docs.cloud.google.com/billing/docs/how-to/budgets-programmatic-notifications).

## Durable run record and state machine

Store one private JSON manifest per run in the GCS ledger bucket. Use Cloud
Storage object generation preconditions for create/update compare-and-swap; a
stale writer must receive a precondition failure, not overwrite a newer cleanup
state ([request preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions)).
Enable object versioning/retention only if the cost and deletion semantics are
approved; no secret, kubeconfig, Terraform state, plan, raw log or profile binary
belongs in the ledger.

Required manifest fields:

- Schema/version, random run ID, dedicated project ID, London region and allowed
  zones; fixed creation/deadline timestamps and maximum lifetime.
- Exact Terraform source revision, image digest, plan checksum, Config Sync/image
  tool versions and reviewed intent checksum.
- State, state generation, lease owner/expiry, last heartbeat and monotonic event
  sequence; allowed states are `prepared`, `provisioning`, `ready`,
  `teardown_requested`, `cleaning`, `auditing`, `closed`, `incomplete`.
- Per-resource expected type, canonical provider URI, discovery key, dependency
  edges, ownership evidence, first-seen/last-seen time, delete attempts/results,
  and independent audit result. Never store bearer credentials or sensitive
  Terraform values.
- Gross cost estimate, measured gross usage, currency, credit balance/expiry
  observation time, net-cash estimate and later billing reconciliation reference.
- Immutable links/checksums to sanitized evidence and an explicit limitation list.

Creation order: validate intent and exact identity -> create/verify persistent
ledger/reaper control -> write `prepared` manifest with create-only generation
precondition -> record fixed deadline and start the five-minute reaper -> only
then create billable run resources. Record `provisioning` and each resource
before/after provider calls. Never reset the deadline or extend expiry in place;
a new attempt gets a new run ID and separately reviewed budget.

The reaper acquires a short manifest lease using generation preconditions, reads
the immutable project/region/ID scope, and ignores closed runs. At deadline it
atomically moves `ready` or `provisioning` to `teardown_requested`, performs the
documented reverse-dependency cleanup, records each response, and re-lists exact
resource types until absence is independently established. `closed` requires a
complete audit plus no billable run resources. Any permission error, ambiguous
resource, unknown type, transient failure past the bounded retry window, or
resource still present becomes `incomplete`; the scheduler keeps retrying and
raises `teardown-failed`. Human recovery can take a manifest lease only by CAS;
the old process cannot concurrently delete. Preserve failure history.

The five-minute scheduler interval is a detection interval, not a precise deadline
or cost cap. Teardown starts no later than minute 40; the 60-minute run deadline
includes the first billable bootstrap resource. Validate control provisioning,
full workload, fault recovery, teardown and audit fit inside this window before
CP-3. If they cannot, stop and get a concrete policy decision before activation.

## Failure tests required before the live drill

| Failure | Required behaviour |
| --- | --- |
| Runner killed before first resource | Prepared manifest/reaper sees no resources; records audited empty result and closes only after independent list checks |
| Runner dies after create succeeds but before ID write | Discover exact run-labeled/deterministically named resource; record then delete; if lookup is ambiguous, halt as incomplete |
| Scheduler invocation or Cloud Run execution fails | Retry boundedly, preserve run; next interval resumes from recorded state; notify failure without closing |
| Duplicate/concurrent reaper execution | One CAS lease wins; stale generation cannot mutate/delete; idempotent delete tolerates already-absent owned resource |
| Runner loses local Terraform state | Reaper cleans exact manifest-discovered resources without state; audit; retain/mark local state unavailable and require empty inventory before new run |
| Terraform destroy partially fails | Preserve state and failures; retry exact dependencies; never remove finalizers or broad-delete project resources |
| Required API/IAM permission missing | Fail preflight before first billable resource when possible; mid-run mark incomplete, notify, retain recovery path |
| Wrong project, region, run ID, deadline, or unknown resource type | Reject before API mutation; tests prove no delete call |
| Cost/credit notification delivery absent | Preflight fails before workload provisioning; teardown notices are also checked |
| Reaper/ledger unavailable | No new run starts; human recovery follows private runbook; mark safety limitation, do not claim automatic cleanup |
| GKE deletion protection on | Reviewed teardown operation changes only this run's cluster setting, then normal delete; no force deletion |
| Audit API is eventually consistent | Require authoritative service-specific get/list checks after deletion; Cloud Asset Inventory is supplementary |

## CP-1B implementation packet

1. Add a versioned run-intent/manifest schema and deterministic validators for ID,
   immutable source/image references, London scope, creation/deadline, expected
   resource types, limits, cost/credit observations and required independent
   cleanup identity. Validate malformed types, `NaN`/boolean numbers, stale or
   overlong expiry, unknown resources and scope mismatch fail closed.
2. Make Terraform resource names and labels run-scoped where supported; add
   positive/negative renderer/plan contract checks. Keep shared project services,
   WIF/budget/alert bootstrap and persistent recovery bucket explicitly outside
   run deletion. No `apply` or cloud API access in this packet.
3. Implement a pure provider-operation interface and testable cleanup planner
   that accepts only exact manifest IDs and a fixed dependency order. Refuse
   unknown types, duplicate IDs under multiple owners, mismatched project/region,
   expired/invalid ledger generations and incomplete discovery. Produce a dry
   run plan with the coverage gaps; no broad-delete fallback.
4. Implement GCS generation-conditional manifest operations and a Cloud Run
   reaper adapter only after the IAM/resource matrix is reviewed. Unit tests use
   a fake API; integration tests in CP-1C use a bounded approved drill.
5. Add lifecycle workflow only after reaper and cleanup path are deployed and
   qualified. The workflow validates plan/source/identity before creating the
   first billable object and must be cancellation-safe.

**CP-1B completion gate:** positive and negative tests, hosted credential-free
validation, exact allowed files and plan, documented API/role matrix, fake
runner-loss and idempotency drills, and a reviewed CP-1C live drill packet.
