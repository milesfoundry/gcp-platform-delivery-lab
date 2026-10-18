# GCP ephemeral-run safety and recovery design

Status: CP-1A/B design and credential-free safety implementation are incomplete.
This document does not authorize enabling APIs, applying Terraform, creating
cloud resources, or spending.

## Decision

The current design candidate uses one disposable Google Cloud project for each
run, with a separate persistent recovery/FinOps project. The latter hosts the
private durable GCS ledger and reviewed reaper image. Each run has its own Cloud
Run Job and unique execution identity in the recovery project. The run project
hosts that run's Cloud Scheduler job and invocation identity. The scheduler
calls `https://run.googleapis.com/v2/projects/{recovery-project}/locations/{location}/jobs/{job}:run`
with an OAuth token and can invoke only its corresponding Job. The Job's fixed
configuration carries its run ID, target project, and ledger object name; the
Scheduler request cannot supply execution overrides. The reaper identity can
clean only the corresponding run project and access only its known ledger
object path. A shared reaper execution identity is disallowed because it would
accumulate access to every run project.

This is a design candidate, not a qualified service or cloud-ready permission
set. The Cloud Scheduler internal-target same-project restriction applies to
internal Cloud Run URLs; this candidate instead calls the Google API endpoint
`run.googleapis.com` with OAuth, which must be qualified end to end. Keep
Terraform state local and private; preserve it through ordinary teardown and
independent audit. If the workflow runner is lost, the reaper acts from the
durable run record and provider APIs without depending on local state. The
recovery project, ledger, image, and audit procedure have persistent storage
and control-plane cost; measure and alert on them, and assign their owner,
retention, backup and eventual project-close procedure.

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
| Private GCS run ledger and per-run Cloud Run Job — recovery/FinOps project owner | One known ledger object path and unique Job/runtime identity per run; image is shared, execution identity is not | Preserve the record through independent audit; reaper cannot delete its Job, identity, bucket, or project | Ledger, bucket, and execution-control costs persist; measure and alert |
| Cloud Scheduler job and invocation identity — run-project owner | Unique scheduler, service account in the same project, fixed OAuth request to the exact recovery-project Job `:run` endpoint | Keep active through workload cleanup and independent audit; delete with the run project only after the external audit gate; scheduler identity has `roles/run.invoker` on that Job alone | Active-run scheduler charge; included in project lifecycle estimate |

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

1. Put each run's billable resources in a disposable, run-dedicated project and
   keep the private ledger and reaper code/image in a separate persistent
   recovery project. Use a per-run reaper Job and execution identity, even if
   each Job uses the same generic image. Grant that identity cleanup access only
   to its run project and manifest path. A single shared Job identity would
   accumulate access to every run project and would remain a cross-run
   compromise boundary. Keep `resourcemanager.projects.setIamPolicy` and project
   deletion outside the reaper role. Keep each run's scheduler trigger bound to
   only that run's Job; verify its project placement and invoker scope. Verify
   all APIs, Fleet/Config Sync operations, node identities, billing alerts and
   inventory/audit flows across this boundary before changing Terraform.
2. Retain one lab project only if an exact conditional-role or deny-policy
   design is shown to cover every destructive and discovery permission for
   network, subnet, router/NAT, GKE, Fleet membership and any future workload
   resources, including negative cross-run tests. Labels and application-side
   ID checks alone do not satisfy this option.

The first option is the working design candidate, not an approved live change.
It adds project provisioning and teardown to the immutable run manifest and
requires updated cost, quota, budget-alert and failure-recovery analysis. The
run project must contain no shared recovery resources. Per-run reaper Jobs and
identities in the recovery project, their scheduler triggers, and the immutable
ledger stay under separate control-plane ownership through independent audit;
they need a post-audit removal path that the run reaper cannot use against
itself or another run. The existing Terraform roots and contract still assume
one dedicated lab project; they must not be treated as implementing per-run
project isolation.

Candidate placement and identity split, pending exact API-role review:

| Boundary | Candidate placement and identity | Required limitation |
| --- | --- | --- |
| Run workload and run scheduler | One new project per run; scheduler job and its invocation service account live there | Scheduler identity calls the Google Cloud Run Jobs API endpoint with OAuth and has `roles/run.invoker` on only that run's recovery-project Job. It has no cleanup access. The API endpoint path and fixed request body are immutable run controls. |
| Reaper execution | One Cloud Run Job per run in the persistent recovery project, built from the same reviewed generic image; unique runtime service account per run | That identity receives only the run-project cleanup role and a conditional ledger-object grant. It cannot change project IAM, invoke other Jobs, or delete its project/Job/identity. Do not share the runtime identity across runs. |
| Run record | Private GCS object path per run in the persistent recovery/FinOps project | Use generation compare-and-swap on a known object name; condition object get/create/update/delete grants on that run path. Do not grant `storage.objects.list`: IAM Conditions cannot restrict that permission to a prefix using `resource.name`. Independent audit uses separate project inventory APIs. Preserve closed-manifest generations for evidence/audit. |
| Cost notifications | Alerts-only budget filtered to the run project under its billing account; Pub/Sub notification topic in the persistent recovery/FinOps project | Billing-account budget management and topic IAM belong to the launcher/operator control plane, not the cleanup identity. Test notification delivery before creating billable run resources. |
| Project and recovery-control reclamation | Separate post-audit control-plane owner; delete the closed run project, budget, per-run reaper Job/identity and trigger only after independent empty-resource audit and billing review | Project deletion and recovery-control deletion are never granted to the run reaper. The owner and exact project/budget IDs must be written into the durable run record and tested for wrong-run refusal. |

The Scheduler service account must be in the same project as its Scheduler job;
for a Google API endpoint, Scheduler uses OAuth. Cloud Run Invoker can be granted
at the Cloud Run Job resource and includes `run.jobs.run`. Those documented
capabilities support the proposed cross-project API call, but no request, OAuth
scope, exact Job binding, Scheduler retry behavior, Fleet registration, GCS
condition, budget lifecycle or post-audit deletion path has yet been implemented
or verified together. Cloud Storage's `resource.name` condition can scope object
access, but cannot scope `storage.objects.list` to a prefix; the reaper must use
the manifest's exact object name and must not list the bucket. Do not treat this
table as an IAM role definition or a cloud-ready plan.

The current bootstrap Terraform budget/channel are attached to the existing
single lab project and do not implement the per-run budget row above. Reconcile
the existing alert configuration and its billing-account owner before designing
the per-run budget lifecycle; do not create a duplicate budget or claim that the
existing Terraform supports disposable projects.

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
Scheduler/Job references: [authenticated HTTP targets](https://docs.cloud.google.com/scheduler/docs/http-target-auth),
[Cloud Run Job execution](https://docs.cloud.google.com/run/docs/execute/jobs),
[Cloud Run IAM roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/run).
Ledger references: [Cloud Storage IAM conditions](https://docs.cloud.google.com/storage/docs/access-control/iam),
[Cloud Storage request preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions).
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
