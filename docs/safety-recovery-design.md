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

### Candidate API permission and authority matrix

This matrix names the API permissions implied by the candidate operations. It is
not a role grant, Terraform implementation, or live authorization. Before CP-1A
can exit, verify the lowest grantable resource for every permission, validate
provider/API side effects in a non-billable test boundary, and define exact custom
roles where predefined roles are too broad. The run cleanup identity is never
allowed to create projects, link billing, change IAM, enable/disable APIs, manage
budgets, or delete recovery controls.

| Actor / phase | Required API operations and permissions to qualify | Binding / authority boundary | Explicit exclusions and unresolved checks |
| --- | --- | --- | --- |
| Project launcher — create disposable project | `resourcemanager.projects.create` | Approved parent folder/organization only | Project creation does not attach billing. Google initially grants the creator `roles/owner`; determine the approved way to remove or constrain that grant before any workload API or billable resource is enabled. |
| Project launcher — attach billing | `billing.resourceAssociations.create` on the billing account and `resourcemanager.projects.createBillingAssignment` on the new project | Billing-account and project scopes, held by the account's separate billing operator | Verify the target billing account, billing state, and project ID before attachment. The reaper cannot link/unlink billing. |
| Bootstrap controller — enable required APIs and create run controls | `serviceusage.services.enable/get/list`; `cloudscheduler.jobs.create/get/list/update/delete`; `iam.serviceAccounts.create/get/list/delete`; `iam.serviceAccounts.actAs` on the target invocation account; `run.jobs.setIamPolicy` on the matching recovery Job to grant its invoker; Scheduler's Google-managed service agent retains `roles/cloudscheduler.serviceAgent` so it can mint the request token | Disposable project for APIs, Scheduler job and invocation account; recovery Job IAM change is constrained to that Job | Do not grant `serviceusage.services.disable` to cleanup. Do not copy existing-project WIF, node identity, Config Sync singleton, budget, or shared controls into the per-run project without a new design. API enablement can create service agents; inventory those as provider-managed dependencies. Verify the service-agent grant and exact IAM write path; never grant the Scheduler service-agent role to the runtime account. |
| Reaper — network discovery and cleanup | `compute.networks.get/list/delete`; `compute.subnetworks.get/list/delete`; `compute.routers.get/list/update/delete`. Removing Cloud NAT is a router update; NAT is router configuration, not an independently deleted router resource. | Project-level custom role in the one disposable run project; the project contains no shared recovery controls | No project IAM mutation, API disablement, route-wide or project-wide bulk delete. Confirm each permission's custom-role support and provider's actual calls. The isolated project is the cross-run boundary; name checks are an additional guard only. |
| Reaper — GKE discovery and cleanup | `container.clusters.get/list/update/delete`; `container.operations.get/list`. `container.clusters.update` is needed for the reviewed deletion-protection transition and node-pool operations. | Same disposable run project | No `container.clusters.getCredentials` and no Kubernetes credentials; no cluster create, IAM grant, or unrelated GKE feature mutation. Verify list/discovery behavior and whether update permissions can be scoped more narrowly. |
| Reaper — Fleet / Config Sync run membership | `gkehub.memberships.get/list/delete`; `gkehub.membershipfeatures.get/list/delete`; `gkehub.operations.get/list` if asynchronous operations require polling | Same disposable run project, after confirming the membership and membership-feature resources inherit the run project boundary | Never delete/update the project-wide `configmanagement` Feature or mutate Fleet/project IAM. Verify provider calls and resource-level grantability. |
| Reaper runtime — read/update durable manifest | At minimum `storage.objects.get` and the write permissions required by the selected JSON API method. Replacing an existing object can require `storage.objects.create` plus `storage.objects.delete`; metadata updates use `storage.objects.update`. Use generation-match preconditions on every state transition. | Conditional binding to the exact `runs/{run_id}/manifest.json` object in the recovery bucket, with uniform bucket-level access | Never grant `storage.objects.list`, bucket administration, object IAM policy, or access to other run paths. Confirm CAS method permissions and retention/versioning semantics in fake and isolated integration tests; retain closed generations for audit. |
| Scheduler execution identity — trigger its reaper | `run.jobs.run` via `roles/run.invoker` bound to only the matching per-run Cloud Run Job | Job resource in the recovery project; Scheduler job and its service account stay in the matching run project | No cleanup permissions and no invocation of any other reaper Job. Verify the Cloud Scheduler service-agent token-creation path, OAuth request, fixed request body, retries, and exact Job IAM binding end to end. |
| Recovery-control provisioner — create per-run reaper | Cloud Run Job get/create/update/delete permissions (`run.jobs.get/create/update/delete`), `run.jobs.setIamPolicy` for only that Job, `iam.serviceAccounts.actAs` for its unique execution service account; Artifact Registry read on the reviewed image repository | Recovery project, Job resource, service account and image repository; separate short-lived provisioner authority | This control-plane identity is not the runtime identity. It must not be shared with the run cleanup identity or included in a run-project role. Verify exact image pull and Job create permissions using the selected deployment method. |
| Cost operator — alerts and topic | `billing.budgets.create/get/list/update/delete` on the billing account; Pub/Sub topic create/get/publish policy operations in the recovery/FinOps project; required project/billing association reads | Billing account and recovery/FinOps project, operated outside both Scheduler and reaper identities | Alert-only budgets are not caps. Discover/import the existing GCP Lab budget and recipient before deciding whether to create per-run budgets. Validate delivery before the first billable resource. |
| Independent auditor — verify absence and reconcile cost | Read-only `get/list` for Compute, GKE and GKE Hub resources in the run project; `resourcemanager.projects.get`; billing association and budget reads; optional Cloud Asset Inventory search | Separate human or read-only audit identity, independent of the reaper runtime and its state | Cloud Asset Inventory is supplementary and eventually consistent. Require service-specific absence checks and a separate reviewer before `closed`; auditor has no delete or IAM-write permission. |
| Post-audit project/control reclamation | `resourcemanager.projects.delete` on the run project (or inherited through an approved parent policy), plus billing-association/budget cleanup and per-run recovery Job, service-account and Scheduler control deletion permissions | Separate named operator after independent empty-resource audit and delayed billing reconciliation | Never granted to the run reaper. Record exact project, budget, Job, service-account and Scheduler IDs in the closed manifest; wrong-run refusal must be tested. Decide whether to delete or retain the empty project and for how long. |

Project-level cleanup is acceptable in this candidate only because each project
is dedicated to one run and contains no recovery, billing, identity, or shared
bootstrap resources. A project-level custom role is still powerful within that
project: preflight must reject unowned resources, cleanup must use exact recorded
IDs, and the independent auditor must report any resource outside the manifest
as an incomplete teardown. Keep `resourcemanager.projects.setIamPolicy`,
`resourcemanager.projects.delete`, `billing.resourceAssociations.*`,
`billing.budgets.*`, `serviceusage.services.disable`, and recovery-project
`run.jobs.delete` out of the runtime identity.

Candidate teardown order is: stop application producers and preserve bounded
evidence; delete the run Config Sync membership feature; delete the Fleet
membership; delete the GKE cluster normally (which owns its node pool); remove
Cloud NAT configuration from the router; delete the router; delete the subnet;
delete the VPC; then independently get/list each recorded type until absent.
Keep the Scheduler and manifest available until the independent audit passes.
Only the separate post-audit operator may then remove the per-run budget,
Scheduler, reaper Job/runtime identity and finally the empty run project after
the delayed billing review.

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
Permission references: [Compute Engine IAM roles and permissions](https://docs.cloud.google.com/compute/docs/access/iam),
[GKE API permissions](https://cloud.google.com/kubernetes-engine/docs/reference/api-permissions),
[GKE Hub roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/gkehub),
[Cloud Run IAM permissions](https://docs.cloud.google.com/run/docs/reference/iam/permissions),
[Cloud Scheduler roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/cloudscheduler),
[Cloud Scheduler authenticated HTTP targets](https://docs.cloud.google.com/scheduler/docs/http-target-auth),
[Service Usage roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/serviceusage),
[Cloud Storage IAM permissions](https://docs.cloud.google.com/storage/docs/access-control/iam-permissions),
[project IAM permissions](https://docs.cloud.google.com/resource-manager/docs/access-control-proj),
and [Cloud Billing budget permissions](https://docs.cloud.google.com/billing/docs/how-to/custom-roles).

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
