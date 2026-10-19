# GCP ephemeral-run safety and recovery design

Status: CP-1A/B design and credential-free safety implementation are incomplete.
This document does not authorize enabling APIs, applying Terraform, creating
cloud resources, or spending.

## Decision

The current design candidate uses one disposable Google Cloud project for each
run, with a separate persistent recovery/FinOps project. The latter hosts the
private durable GCS ledger, private Terraform state, and reviewed reaper image.
Each run has its own Cloud Run Job, execution identity, Cloud Scheduler job, and
Scheduler invocation identity in the recovery project. Write a create-only
durable `control_bootstrapping` manifest before creating recovery controls.
Verify the controls and transition the manifest to `prepared` before creating
or linking the disposable project. The Scheduler calls
`https://run.googleapis.com/v2/projects/{recovery-project}/locations/{location}/jobs/{job}:run`
with an OAuth token and can invoke only its corresponding Job. The Job's fixed
configuration carries its run ID, target project, and ledger object name; the
Scheduler request cannot supply execution overrides. Its runtime identity can
clean only the corresponding run project and access only its known ledger object
path. A shared reaper execution identity is disallowed because it would
accumulate access to every run project.

### Per-run identity permission boundary

These are source-verified permission candidates for the Terraform packet, not
an applied or qualified IAM policy. The reaper receives no project-creator,
project-IAM-admin, billing-account-admin, Cloud Run deployer, Scheduler admin,
or recovery-control deletion role.

| Principal | Candidate grant | Resource boundary and qualification |
| --- | --- | --- |
| Per-run reaper Job identity | `storage.objects.get`, `storage.objects.create`, `storage.objects.delete` in a recovery-project custom role | Bind on the private ledger bucket with uniform bucket-level access and an IAM Condition requiring the exact `runs/{run_id}/manifest.json` object resource name. Do not grant `storage.objects.list`; GCS does not filter list results by object-name conditions. Replacing a generation-matched ledger object requires both create and delete permissions. |
| Per-run reaper Job identity | `resourcemanager.projects.get`, `resourcemanager.projects.deleteBillingAssignment`, `resourcemanager.projects.delete` | Define a project-level custom role in the disposable project and bind it only on that project. Confirm all three permissions are supported in custom roles in the target organization before activation. The role disappears with its project. Do not grant at the folder or organization. |
| Per-run Scheduler invocation identity | `roles/run.invoker` | Bind with `google_cloud_run_v2_job_iam_member` on only the matching Job. Use an OAuth token for the `run.googleapis.com` Jobs API; the invocation account must be in the Scheduler project. It cannot pass execution overrides. |
| Trusted run/bootstrap operator | `iam.serviceAccounts.actAs` on the two per-run identities and policy-writing permissions for the exact resource policies | This identity creates the recovery controls and binds the run-project role. It is separate from both runtime identities and is not available to either job. |

Google documents the exact project permissions for [project deletion](https://docs.cloud.google.com/resource-manager/reference/rest/v3/projects/delete), [billing unlink](https://docs.cloud.google.com/billing/docs/how-to/modify-project), and [billing-info reads](https://docs.cloud.google.com/billing/docs/reference/rest/v1/projects/getBillingInfo). Cloud Storage object replacement requires create and delete, and an object-name IAM Condition requires uniform bucket-level access ([Storage IAM permissions](https://docs.cloud.google.com/storage/docs/access-control/iam-permissions), [Storage IAM Conditions](https://docs.cloud.google.com/storage/docs/access-control/iam)). Cloud Run Jobs support job-level invoker bindings ([Terraform IAM resource](https://registry.terraform.io/providers/hashicorp/google/8.5.0/docs/resources/cloud_run_v2_job_iam)).

This is a design candidate, not a qualified service or cloud-ready permission
set. The Scheduler and invocation account share the persistent recovery project;
the target is the Google Cloud Run Jobs API endpoint, which uses OAuth. Qualify
the exact request, token, job-level invoker binding, retry behavior and API
enablement end to end. The Scheduler trigger must be recurrent through the
post-deadline audit and retirement window, not a one-shot expiry trigger:
`jobs.run` dispatches a Cloud Run Job execution, while this entrypoint can exit
successfully without mutation when the durable state is not yet eligible. Keep
the trigger until the independent closure audit permits control reclamation;
each Cloud Run execution holds its own execution-scoped ledger lease, so only
one fresh invocation can mutate the run at a time. See Google's [Cloud Run Job execution
API](https://docs.cloud.google.com/run/docs/execute/jobs) and [Cloud Scheduler
run method](https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs/run).
Keep Terraform state in a private per-run GCS bucket in the recovery project,
separate from the manifest ledger, and retain it through ordinary teardown and
independent audit. The reaper acts from the durable run
record and provider APIs, never Terraform state, so runner/state loss does not
block cleanup. If recovery-project IAM or its Google API control plane is
unavailable, use the separate human operator recovery path after access returns;
do not claim recovery from total project/control-plane loss. The recovery
project, ledger, state buckets, images, per-run Jobs/Schedulers and audit
procedure have ongoing cost. Measure and alert on them, and assign their owner,
retention, private backup, and eventual project-close procedure.

The source implementation is `recovery/terraform`. It creates the per-run
reaper and Scheduler service accounts, the digest-pinned Cloud Run Job, an
exact-Job `roles/run.invoker` grant, a five-minute London Scheduler trigger,
and exact-manifest-object GCS access. The reaper Job is single-task, single
parallel, has a 90-second timeout below its two-minute ledger lease, and has
zero task retries; later Scheduler polls resume from the ledger. Terraform
requires a separately provisioned private per-run state
bucket through `-backend-config` using `backend.hcl.example`; it must be in the
recovery project, have uniform bucket-level access, and stay separate from the
ledger. Verify the manifest bucket is also in the recovery project with uniform
bucket-level access before applying its conditional IAM grant.
Project-retirement IAM is disabled by default because the run project does not
exist when controls are bootstrapped. Enable that phase only after the project
exists and custom-role permission support is approved. Job deletion protection
also remains on until reviewed control reclamation. This Terraform source is a
candidate for hosted validation; it has not been applied or cloud-qualified.

Recovery bootstrap must be registered before its first create. Write a
create-only `control_bootstrapping` manifest with deterministic expected Job,
service-account, Scheduler, project and budget identifiers before creating any
run control. Record each control create request and response. If the runner is lost
before the reaper Job and Scheduler are both verified, no disposable project or
billable workload resource may be created; the recovery-project operator resumes
or removes the partial controls using the manifest and audit APIs. Only after
the trigger is verified may project creation/linking proceed. The post-audit
operator deletes these recovery controls after the run is closed. This leaves a
small control-plane bootstrap failure window recovered manually rather than by
the not-yet-created per-run trigger.

The GCS ledger is independent of the runner and Kubernetes management plane, but
shares the GCP project and provider control plane. If project IAM, Cloud Scheduler,
Cloud Run or GCS is unavailable, the independent operator recovery path is the
existing privileged human workflow after access is restored. Do not claim recovery
from total project/control-plane loss. Cloud Asset Inventory is an additional
discovery source, not the sole cleanup proof: Google documents its current asset
data as eventually consistent and best effort ([asset types](https://docs.cloud.google.com/asset-inventory/docs/asset-types)).

### Human recovery and host-loss boundary

Define four separate duties before activation: the GitHub/run launcher, the
recovery-control operator, the project/billing operator and the read-only
independent auditor. The human recovery operator uses a separately authenticated
MFA identity, private runbook and recovery-project access; the cleanup runtime
identity is never reused. The exact human principals, parent-folder grant,
billing-account grant and backup administrators are unresolved until the
account owner records them. No new run starts without that mapping.

The recovery path is:

1. Stop new launches and preserve the failing run's immutable manifest generation,
   run ID, control IDs, project ID and last state generation. If GCS is available,
   read the exact object; use an auditor/operator identity to list manifests when
   the launcher does not know an object path.
2. If the recovery project or ledger is unavailable, restore the manifest from
   the separately administered private backup. Restore state only into a new
   generation or recovery copy; never overwrite the last retained generation.
   Record the source generation, checksum, state lineage/serial and restore
   operator. The backup location, encryption key owner and restore procedure
   must be chosen and recovery-tested before activation.
3. Compare the manifest's expected control IDs with exact Cloud Run Job,
   Scheduler and service-account reads. Resume incomplete control bootstrap or
   remove only recorded partial controls after confirming no run project exists
   and no create request is unresolved. A `prepared` record with no project-create
   request is an empty run; the Job makes no project mutations. The operator
   records the no-project audit and reclaims its Scheduler, Job and identities.
4. For project-create or billing-link requests with a lost response, the project/
   billing operator reconciles by the fixed project ID and billing account. The
   per-run reaper can only unlink billing and delete that exact project after a
   passed workload audit and empty inventory; it cannot create projects, attach
   billing, or change project IAM. Ambiguous create/link state remains
   `incomplete` until the operator reconciles it; no API enablement or workload
   creation continues.
5. If the recovery project itself is lost, recreate its control plane from
   reviewed infrastructure source, restore the manifest/state backup, inspect
   every recorded run project through read-only service APIs, then resume cleanup
   with a newly reviewed per-run job/trigger. This candidate covers loss of the
   recovery project while the separate backup project, organization IAM and
   Google APIs remain available. It does not cover loss of the organization,
   billing account, backup project, or Google Cloud control plane; stop and use
   Google support/account recovery for those events.

The recovery and backup projects, state/ledger retention, monitoring and empty
project retention have real cost. Their owner, separate administrator, billing
source, restore objective, recovery-time objective and retention period must be
recorded before CP-1A exits. No such values are currently qualified.

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
| Run API enablements — `google_project_service.lab` in the lab Terraform root | New run project ID plus exact service name; only enable after expiry controls are verified; service agents created as side effects are derived run assets | Keep enabled during cleanup; disable only after workload audit and before project reclamation if needed; independently list service agents and APIs | API enablement usually has no direct charge; service-agent resources and enabled services may have downstream costs |
| VPC — lab Terraform root | Add `lab_run_id` label if the provider/API supports it; otherwise unique VPC name; exact project/network self-link | Delete after subnet, router/NAT and cluster dependencies; verify exact VPC absent | Project resource, run lifetime |
| Subnet and secondary ranges — lab root | Run-specific name and VPC URI; confirm tag support | Delete after cluster and router/NAT; audit exact self-link | Project resource, run lifetime |
| Router and Cloud NAT — lab root | Run-specific names and router/subnetwork URIs; confirm tag support | Delete NAT, then router, after cluster; audit NAT config and router absence | Billable while present; include setup/deletion interval |
| GKE cluster and zonal node pool — lab root | Cluster resource labels include run ID; unique cluster/name; record exact cluster ID, each node-pool ID and all zones | Drain/stop application producers, export bounded evidence, remove run Config Sync membership/config in reviewed dependency order, then delete exact node pools and cluster; audit cluster, pools and every node instance absent | Main billable resource; deadline begins before first billable bootstrap resource |
| Fleet membership, Config Sync membership feature and `configmanagement` Feature — lab Terraform root | All are per-run in the disposable project; exact project/location/name and membership IDs go in the manifest. The Fleet Feature is a project-scoped singleton, but is not shared across disposable projects | Remove Config Sync membership feature, then Fleet membership, then the now-unused per-run Feature; independently verify each exact ID absent before deleting the cluster/network | Config Sync/service charges and API use are run costs; confirm billing treatment and deletion semantics |
| Node service account and project role — lab Terraform root | Create one run-specific node identity in that run's project with only `roles/container.defaultNodeServiceAccount`; record service-account email and IAM binding as run-owned | Do not let the reaper change IAM. Remove through separately authorized project reclamation after GKE is absent; project deletion is the final authority boundary | No application cloud permissions; role binding is project scoped and disappears with the project |
| Future Pub/Sub topics/subscriptions and GCS report objects — CP-2 workload Terraform | Every topic/subscription/object carries run ID or lives under a dedicated run prefix; store exact full resource URI/object generation | Stop publishers, drain/delete subscriptions and queues; export evidence; delete only run-owned object generations after evidence verification; audit queues and versions absent | Include retained versions/egress/operations; evidence copies are separately allowlisted |
| Bootstrap WIF pool/provider, CI service account, IAM grants — bootstrap root | Explicit bootstrap owner record, not a run ID | Keep through active run and audit; remove only in reviewed bootstrap/project-close sequence after run inventory is empty | Retained exception until explicit removal; protect from janitor |
| Existing project billing budget and Monitoring channel/policies — bootstrap/account owner | Existing verified object IDs; import/reconcile before managing, never create competing objects blindly | Keep alerting through teardown and delayed billing review; remove only through explicit account closeout | Budget is a warning, not a cap; recipient config/state remain private |
| Private GCS run ledger and per-run Cloud Run Job — recovery/FinOps project owner | One known ledger object path and unique Job/runtime identity per run; image is shared, execution identity is not | Preserve the record through independent audit; reaper cannot delete its Job, identity, bucket, or project | Ledger, bucket, and execution-control costs persist; measure and alert |
| Per-run Cloud Scheduler job and invocation identity — recovery-project control-plane owner | Unique scheduler and service account in the recovery project, fixed OAuth request to the exact matching recovery-project Job `:run` endpoint | Create before the disposable project; retain through workload cleanup and independent audit. Scheduler identity has `roles/run.invoker` on its Job alone. Delete only in post-audit reclamation. | Persistent control-plane resource during a run; include Scheduler and Job charges in cost estimate. A bootstrap failure before the scheduler is verified requires operator recovery and must not create the disposable project. |
| In-cluster GitOps resources — `gitops/platform` and `gitops/apps/report` | Namespace, quota, NetworkPolicy, Role/RoleBinding, Config Sync root, RepoSync and report ConfigMap belong to the per-run cluster; record repository revision and root IDs | Capture bounded sanitized evidence; remove RepoSync/root and Config Sync membership in reviewed order, then delete the cluster; independently confirm no workload survives cluster removal | Cluster runtime cost; evidence copies follow their separate retention policy |
| GKE and Service Usage derived resources — Google-managed service agents, node VMs/disks, addresses, logs/metrics and artifacts | Associate with the run by exact parent project and GKE cluster/node-pool IDs. Record provider-returned IDs and documented derived resource classes when no stable child ID is returned | Stop producers, remove their owning GKE/Fleet/network resources in dependency order, then independently query Compute, GKE, GKE Hub, Service Usage and billing inventory. Any unexplained resource keeps the run `incomplete` | Include node disks, addresses, logs/metrics and retained artifacts in estimate and later billing reconciliation |
| Terraform state — currently two local backends; candidate private remote state in recovery project | Dedicated bucket per run, exact state object, lineage/serial, generation and source root recorded in manifest; independent backup has its own recovery ID | State is not cleanup authority. Retain through empty-resource audit and billing review; verify export/restore before later destruction; delete only by state owner after evidence retention | State includes sensitive values; version and backup retention cost; never attach state to artifacts or ledger |

### Current Terraform and GitOps inventory mapped to the candidate

The following are the exact source addresses in this repository today. Their
current ownership is a single existing project; the candidate placement column
is the required split before any per-run project lifecycle can be implemented.
No address in this table means that a cloud object exists or has been applied.

| Current source address / files | Present role | Candidate owner and inventory treatment |
| --- | --- | --- |
| `google_compute_network.lab`, `google_compute_subnetwork.lab`, `google_compute_router.lab`, `google_compute_router_nat.lab` — `infrastructure/lab/main.tf` | VPC, subnet with pod/service ranges, Cloud Router and NAT | Per-run lab root in disposable project; record full IDs, router NAT config, dependencies and generated addresses; cleanup by exact IDs |
| `google_container_cluster.lab`, `google_container_node_pool.lab` — `infrastructure/lab/main.tf` | Regional GKE cluster with three zonal nodes and managed pool | Per-run lab root; record cluster ID, node-pool ID, zones and deletion-protection state. Managed VMs/disks and control-plane services are derived assets and must be audited |
| `google_gke_hub_membership.lab`, `google_gke_hub_feature_membership.config_sync` — `infrastructure/lab/main.tf` | Registers cluster and applies Config Sync configuration | Per-run lab root; exact membership and feature-membership IDs; remove configuration before membership and cluster |
| `google_project_service.bootstrap[*]` — `bootstrap/terraform/main.tf` | Enables IAM, STS, Resource Manager, Billing Budgets, Monitoring and Logging in the persistent bootstrap/recovery project | Remains control-plane owned. It no longer enables workload APIs in a run project |
| `google_project_service.lab[*]` — `infrastructure/lab/main.tf` | Enables Compute, GKE, GKE Hub, Config Management, Connect, IAM, Service Usage, Logging, Monitoring, Storage and Pub/Sub in the run project | Per-run project resources; enable only after recovery controls, project creation, billing and budget checks pass. Record service names and generated service-agent side effects; reaper never disables APIs |
| `google_service_account.lab_nodes`, `google_project_iam_member.lab_node_service` — `infrastructure/lab/main.tf` | Run-specific node SA and project-level default node role | Created in each disposable project; reaper cannot change IAM. Record separately under project-reclamation ownership; final project deletion removes them |
| `google_gke_hub_feature.config_sync` — `infrastructure/lab/main.tf` | `configmanagement` Feature in the run project | Per-run Fleet Feature prerequisite to membership feature; manifest cleanup permission includes `gkehub.features.get/delete`; creation belongs to run provisioner. |

| `google_service_account.ci`, `google_iam_workload_identity_pool.github`, `google_iam_workload_identity_pool_provider.github`, `google_service_account_iam_member.federation`, `google_project_iam_member.approved_apply_roles[*]` — bootstrap root | GitHub OIDC federation and current project apply grants | Keep under separate provisioner/control-plane ownership. The project creator/billing linker is separate authority; its exact parent and billing scope, initial project Owner grant handling and short-lived credential path remain unresolved |
| `google_monitoring_notification_channel.cost_email["enabled"]`, `google_billing_budget.cost_alerts["enabled"]`, `google_monitoring_alert_policy.lifecycle_events[*]` — bootstrap root | Existing project email channel, GBP alert budget and six log alert policies | Reconcile the verified existing project budget; per-run project alert budgets and delivery route are created by cost operator, not the reaper. Preserve evidence of tested recipient delivery and delayed cost reconciliation |
| `gitops/platform/{namespace,quota,network-policy,role,role-binding,root}` and `gitops/apps/report/{repo-sync,config-map}` | In-cluster objects reconciled by Config Sync | Per-run cluster contents. Record source revision and root inventory; preserve bounded evidence, then remove through Config Sync and cluster teardown |

The bootstrap and lab roots now separate persistent controls from run-owned
resources in source. Recovery/control-plane Terraform still needs to own the
ledger, per-run state buckets, reaper image/Jobs/Schedulers and provisioner
controls; the lab root owns run APIs, network, GKE, Fleet membership/Feature and
node identity; the project launcher and cost operator own project creation,
billing attachment and project-filtered budgets. Keep source roots and state
owners separate so `terraform destroy` cannot cross these boundaries. These
Terraform changes have not been applied; existing state and provider-call
qualification remain gates.

The exact provider permission set for the reaper is a CP-1B exit check. Derive
permissions from the API operations above and test denied operations. Do not use
project Owner/Editor or a broad project-admin role. Separate the GitHub federated
provisioning identity, GKE node identity, janitor execution identity and human
recovery identity. The reaper must not receive `resourcemanager.projects.setIamPolicy`:
Google authorizes that permission at project scope, so it could rewrite unrelated
project grants ([permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/resourcemanager)).
In a disposable-project design, the node service account and its node role are
created per run and recorded as run-owned project resources; the reaper still
does not mutate project IAM. GKE cluster deletion and Compute network deletion have
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
| Run workload and project lifecycle | One new project per run; only run-owned workload, API enablement, Config Sync Feature, node identity and derived service agents live there | The project contains no ledger, Terraform state, reaper, scheduler, budget-notification topic or shared recovery identity. Project create/link and final project deletion belong to separate operator control-plane steps. |
| Per-run expiry trigger | One Scheduler job and invocation service account per run in the persistent recovery project; create and verify before creating/linking the run project | Scheduler identity calls the matching recovery-project Cloud Run Jobs API endpoint with OAuth and has `roles/run.invoker` on only that run's Job. No cleanup access or overrides. If trigger bootstrap is incomplete, operator recovers it and the run project is not created. |
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

The current bootstrap Terraform budget/channel remain attached to its
configured project and do not implement the per-run budget row above. Reconcile
the existing alert configuration and its billing-account owner before designing
the per-run budget lifecycle; do not create a duplicate budget or claim that the
existing Terraform supports disposable projects.

Project lifecycle is a separate authority boundary: project creation requires
`resourcemanager.projects.create` on the parent, and the Resource Manager create
method does not attach a billing account. Billing association has its own
permission and verification step. The project-reclaimer path now journals a
billing unlink, then permits exact-project deletion only after a passed
independent workload audit and all workload resources are marked deleted. Its
poller keeps the operation unresolved while Resource Manager reports
`DELETE_REQUESTED`; it resolves only after exact project lookup returns 404.
Recovery-control reclamation is gated on that reconciled absence, and final
closure still requires a separate complete project/billing/budget/control audit.
The manual operator lane uses the named `operator:project-reclaimer` lease and
verifies its Google user token through UserInfo. The scheduled Cloud Run lane
now has a separate source-level runtime factory and verifies its per-run
service-account ID token against the API access token. Both lanes record their
verified principal on the retirement intents, and the ledger contract requires
the same principal across billing unlink and project deletion. The GCS adapter
accepts either source only when each request is bound to the configured email
and principal ID; the Cloud Run source constructs explicit metadata-server
credentials without ADC lookup. Neither lane is deployed or live-qualified. For cost
monitoring, the Budget API can scope an alerts-only budget to one project under
the billing account, while Google recommends keeping
programmatic budget-notification topics in a FinOps administration project.
These are design capabilities, not proof that the current account has the
required parent or billing permissions.

### Reclaimer principal split — required before deployment

The current `operator:project-reclaimer` implementation is a human-operated
retirement path. It accepts only a Google user access token verified through
UserInfo. It must not be used as the credential path for the scheduled Cloud
Run janitor: Cloud Run calls Google APIs as its configured service-account
service identity, and a human refresh token is neither that identity nor a
durable unattended credential.

Keep the human recovery operator as a separate, MFA-protected fallback and
control-plane approver. The scheduled per-run janitor uses the unique
service-account identity configured on its exact Cloud Run Job. The runtime
must obtain both API access and service-account identity tokens from the Cloud
Run metadata server, with a fixed configured audience for the identity token.
Before any mutation or ledger write, verify the Google signature, issuer
`https://accounts.google.com`, exact audience, expiry, verified service-account
email and immutable numeric `sub` recorded in the run controls. Bind that proof
to the same metadata service identity that provides the API access token; fail
closed if either token, the email or numeric identity differs. Use a distinct
ledger lease identity for the job, while preserving `operator:project-reclaimer`
for the human recovery path. Never put a user refresh token in the Job or
Scheduler configuration.

`CloudRunServiceAccountTokenSource` and
`build_cloud_run_project_reclaimer_runtime()` now implement the source path.
They construct explicit metadata-server access and ID-token credentials, verify
the ID token's signature/audience/issuer/expiry/email/numeric subject, and bind
that proof to the access token from the same default service identity. The GCS
credential wrapper and project lifecycle adapter accept that verified principal.
Retirement leases now distinguish `operator:project-reclaimer` with a human
user from `janitor:google-service-account-{numeric-sub}:{execution-id}`; the
execution ID is unique to one Cloud Run Job execution while the operation
records preserve the verified service-account principal across billing unlink
and project deletion. Fake tests cover wrong audience,
issuer, email, numeric subject, expiry, access-token mismatch, refresh failure,
principal change, and lease/principal mismatch. The Google signature verifier
itself and metadata server have not been exercised in Cloud Run. Deployment
must still prove that each job can access only its own ledger record and exact
run project. IAM isolation and the independent janitor remain unimplemented and
unqualified.

This follows Google's documented [Cloud Run service identity](https://docs.cloud.google.com/run/docs/securing/service-identity)
and [service-account ID token claims](https://docs.cloud.google.com/docs/authentication/token-types).

### Candidate API permission and authority matrix

This matrix names the API permissions implied by the candidate operations. It is
not a role grant, Terraform implementation, or live authorization. Before CP-1A
can exit, verify the lowest grantable resource for every permission, validate
provider/API side effects in a non-billable test boundary, and define exact custom
roles where predefined roles are too broad. The run cleanup identity is never
allowed to create projects, link billing, change IAM, enable/disable APIs, manage
budgets, or delete recovery controls.

Google's current custom-role support catalogue marks the listed Compute network,
subnetwork and router permissions; GKE cluster and operation permissions; GKE
Hub Feature, membership, membership-feature and operation permissions; Cloud Run
Job permissions; and Cloud Storage object permissions as supported for custom roles.
That means these permissions can be represented in a custom role. It does not
prove that a binding can be attached at the desired resource level, that the
Terraform provider uses only these API calls, or that a live identity is denied
cross-boundary actions. Those remain qualification gates.

| Actor / phase | Required API operations and permissions to qualify | Binding / authority boundary | Explicit exclusions and unresolved checks |
| --- | --- | --- | --- |
| Project launcher — create disposable project | `resourcemanager.projects.create`; `resourcemanager.projects.get` for exact-ID reconciliation | Approved parent folder/organization only | Project creation does not attach billing. Google initially grants the creator `roles/owner`; determine the approved way to remove or constrain that grant before any workload API or billable resource is enabled. |
| Project launcher — attach billing | `billing.resourceAssociations.create` on the billing account and `resourcemanager.projects.createBillingAssignment` on the new project | Billing-account and project scopes, held by the account's separate billing operator | Verify the target billing account, billing state, and project ID before attachment. The reaper cannot link/unlink billing. |
| Bootstrap controller — enable required APIs and create run controls | `serviceusage.services.enable/get/list` in each required project; `cloudscheduler.jobs.create/get/list/update/delete`; `iam.serviceAccounts.create/get/list/delete`; `iam.serviceAccounts.actAs` on the target invocation account; `run.jobs.setIamPolicy` on the matching recovery Job to grant its invoker; Scheduler's Google-managed service agent retains `roles/cloudscheduler.serviceAgent` so it can mint the request token | Scheduler job and invocation account plus matching Job IAM change are in the recovery project. Enable run-project APIs only after its expiry trigger is verified. | Do not grant `serviceusage.services.disable` to cleanup. Keep shared WIF/provisioner identities, ledger/state, budget-notification topic and reaper controls out of the run project. Create the node identity, Config Sync Feature and run-only APIs inside the run project. API enablement can create service agents; inventory those as provider-managed dependencies. Verify the service-agent grant and exact IAM write path; never grant the Scheduler service-agent role to the runtime account. |
| Reaper — network discovery and cleanup | `compute.networks.get/list/delete`; `compute.subnetworks.get/list/delete`; `compute.routers.get/list/update/delete`. Removing Cloud NAT is a router update; NAT is router configuration, not an independently deleted router resource. | Project-level custom role in the one disposable run project; the project contains no shared recovery controls | No project IAM mutation, API disablement, route-wide or project-wide bulk delete. Confirm each permission's custom-role support and provider's actual calls. The isolated project is the cross-run boundary; name checks are an additional guard only. |
| Reaper — GKE discovery and cleanup | `container.clusters.get/list/delete`; `container.nodePools.get/list/delete`; `container.operations.get/list`. `container.clusters.update` is required for node-pool deletion. | Same disposable run project | No `container.clusters.getCredentials` and no Kubernetes credentials; no cluster create, IAM grant, or unrelated GKE feature mutation. Terraform's `deletion_protection` is a Terraform-state guard, not a GKE API field; the REST cleanup adapter does not toggle it. Keep the Terraform guard enabled during operation and require the exact-cluster cleanup review before an API delete. If teardown uses Terraform, explicitly set it false and apply that state change before destroy. |
| Reaper — Fleet / Config Sync run membership | `gkehub.memberships.get/list/delete`; `gkehub.membershipfeatures.get/list/delete`; `gkehub.operations.get/list` if asynchronous operations require polling | Same disposable run project, after confirming the membership and membership-feature resources inherit the run project boundary | Delete only the per-run `configmanagement` Feature after its membership is absent; never mutate Fleet or project IAM policies from the reaper. Verify provider calls and resource-level grantability. |
| Reaper — per-run Config Sync Feature | `gkehub.features.get/delete`; `gkehub.operations.get/list` where deletion is asynchronous. `gkehub.features.create` belongs to the run provisioner, not reaper | Exact Feature in the disposable run project; remove only after membership feature and membership are absent | Never touch a feature in another project; verify provider API calls and project-level scope. The API parent is the project/location (`projects/*/locations/*`). |
| Reaper — Pub/Sub workload resources | `pubsub.subscriptions.get/delete`; `pubsub.topics.get/delete` | Exact per-run topic/subscription IDs in the disposable project | Delete subscriptions before topics; Pub/Sub delete is a direct exact-resource REST request. Confirm the API project's full inventory and IAM boundary before live use. See [subscription delete](https://docs.cloud.google.com/pubsub/docs/reference/rest/v1/projects.subscriptions/delete) and [topic delete](https://docs.cloud.google.com/pubsub/docs/reference/rest/v1/projects.topics/delete). |
| Reaper — run-project GCS objects | `resourcemanager.projects.get`; `storage.buckets.get`; `storage.objects.get/delete` | Exact project-bound bucket/object ID; verify bucket project number and London location before object access | Object versions use the exact generation; live-object deletion uses `ifGenerationMatch`. Set zero soft-delete retention when creating ephemeral buckets; the default is seven days. The adapter rejects active soft-delete retention, since retained objects cannot be immediately purged. Inventory must list `softDeleted=true` separately when retention is active. See [bucket get](https://docs.cloud.google.com/storage/docs/json_api/v1/buckets/get), [object delete](https://docs.cloud.google.com/storage/docs/json_api/v1/objects/delete), [object list](https://docs.cloud.google.com/storage/docs/json_api/v1/objects/list), and [soft-delete overview](https://docs.cloud.google.com/storage/docs/soft-delete). |
| Reaper runtime — read/update durable manifest | At minimum `storage.objects.get` and the write permissions required by the selected JSON API method. Replacing an existing object can require `storage.objects.create` plus `storage.objects.delete`; metadata updates use `storage.objects.update`. Use generation-match preconditions on every state transition. | Conditional binding to the exact `runs/{run_id}/manifest.json` object in the recovery bucket, with uniform bucket-level access | Never grant `storage.objects.list`, bucket administration, object IAM policy, or access to other run paths. Confirm CAS method permissions and retention/versioning semantics in fake and isolated integration tests; retain closed generations for audit. |
| Scheduler execution identity — trigger its reaper | `run.jobs.run` via `roles/run.invoker` bound to only the matching per-run Cloud Run Job | Job resource, Scheduler job and Scheduler service account all stay in the recovery project; use one invocation identity per run | No cleanup permissions and no invocation of any other reaper Job. Verify the Cloud Scheduler service-agent token-creation path, OAuth request, fixed request body, retries, and exact Job IAM binding end to end. |
| Recovery-control provisioner — create per-run reaper | Cloud Run Job get/create/update/delete permissions (`run.jobs.get/create/update/delete`), `run.jobs.setIamPolicy` for only that Job, `iam.serviceAccounts.actAs` for its unique execution service account; Artifact Registry read on the reviewed image repository | Recovery project, Job resource, service account and image repository; separate short-lived provisioner authority | This control-plane identity is not the runtime identity. It must not be shared with the run cleanup identity or included in a run-project role. Verify exact image pull and Job create permissions using the selected deployment method. |
| Cost operator — alerts and topic | `billing.budgets.create/get/list/update/delete` on the billing account; Pub/Sub topic create/get/publish policy operations in the recovery/FinOps project; required project/billing association reads | Billing account and recovery/FinOps project, operated outside both Scheduler and reaper identities | Alert-only budgets are not caps. Discover/import the existing GCP Lab budget and recipient before deciding whether to create per-run budgets. Validate delivery before the first billable resource. |
| Independent auditor — verify absence and reconcile cost | Read-only `get/list` for Compute, GKE and GKE Hub resources in the run project; `resourcemanager.projects.get`; billing association and budget reads; optional Cloud Asset Inventory search | Separate human or read-only audit identity, independent of the reaper runtime and its state | Cloud Asset Inventory is supplementary and eventually consistent. Require service-specific absence checks and a separate reviewer before `closed`; auditor has no delete or IAM-write permission. |
| Post-audit project/control reclamation | `resourcemanager.projects.delete` on the run project (or inherited through an approved parent policy), plus billing-association/budget cleanup and per-run recovery Job, service-account and Scheduler control deletion permissions | Separate named operator after independent empty-resource audit and delayed billing reconciliation | Never granted to the run reaper. Record exact project, budget, Job, service-account and Scheduler IDs in the closed manifest; wrong-run refusal must be tested. Decide whether to delete or retain the empty project and for how long. |

The Google provider documents `google_container_cluster.deletion_protection` as
a Terraform destroy guard evaluated from Terraform state, and says to apply
`false` before a Terraform destroy. This is distinct from a GKE API field or
IAM deny policy. The REST adapter calls the normal cluster DELETE API directly,
so its exact-cluster review gate is a separate safety control. See the
[provider resource reference](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/container_cluster).

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
membership; delete the per-run `configmanagement` Feature after its membership
is gone; delete the GKE cluster normally (which owns its node pool); remove
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
[create Fleet Feature API](https://docs.cloud.google.com/kubernetes-engine/fleet-management/docs/reference/rest/v1alpha/projects.locations.features/create),
[Cloud Run IAM permissions](https://docs.cloud.google.com/run/docs/reference/iam/permissions),
[Cloud Scheduler roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/cloudscheduler),
[Cloud Scheduler authenticated HTTP targets](https://docs.cloud.google.com/scheduler/docs/http-target-auth),
[Service Usage roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/serviceusage),
[Cloud Storage IAM permissions](https://docs.cloud.google.com/storage/docs/access-control/iam-permissions),
[custom-role permission support](https://docs.cloud.google.com/iam/docs/custom-roles-permissions-support),
[project IAM permissions](https://docs.cloud.google.com/resource-manager/docs/access-control-proj),
and [Cloud Billing budget permissions](https://docs.cloud.google.com/billing/docs/how-to/custom-roles).

## Durable run record and state machine

Store one private JSON manifest per run in the GCS ledger bucket. Use Cloud
Storage object generation preconditions for create/update compare-and-swap; a
stale writer must receive a precondition failure, not overwrite a newer cleanup
state ([request preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions)).
The credential-free `scripts/run_ledger.py` now implements an exact-path GCS
adapter: create uses generation-match `0`, replacement uses the previously-read
generation, and download is conditioned on the generation observed by reload.
It never lists or deletes ledger objects. `scripts/test_run_ledger.py` exercises
lost create responses, exact-run reconciliation, stale writers, provider-side
races, invalid/corrupt manifests and the precondition arguments through a fake
store. This is adapter-level evidence only; no GCS bucket, IAM binding, SDK
credentials, object versioning, retention or private backup has been configured
or exercised. The Cloud Storage Python client is version-pinned in
[`scripts/requirements-recovery.txt`](../scripts/requirements-recovery.txt).
Confirm its exact permissions and retry behavior with the isolated integration
test before activation.
Each ledger object now wraps the manifest in `portfolio.gcp-ledger.v1` metadata:
an immutable intent checksum, monotonic event history, lease owner/expiry, and
last heartbeat. Manifest mutation requires the active lease owner and exact GCS
generation. Leases are bounded to 15 seconds–5 minutes; expired leases can be
reclaimed by CAS, while foreign or stale writers fail closed. The 1024-event
history is never truncated; a full record stops mutation for operator review.
The GCS object's generation returned by the provider is the authoritative
generation token and is carried by the in-memory ledger snapshot, not guessed
or copied into the object payload.
Each Cloud Run execution uses a unique lease owner containing its injected
`CLOUD_RUN_EXECUTION` name, so overlapping Scheduler dispatches do not share a
lease identity. This lease arbitrates manifest writers only; it does not fence
an already-sent Google Cloud request. Before a recovery controller performs any resource
operation, it must persist the exact operation intent and deterministic resource
ID, recheck lease ownership immediately before the call, record the response, and
reconcile unresolved requests by exact ID after takeover. Cleanup must keep
retrying while a create/delete outcome is unresolved. Delayed provider responses
and runner loss during those calls remain fake-provider acceptance cases; this
ledger slice does not yet prove them.

The manifest now contains an append-only provider-operation journal. A request
intent records a deterministic attempt ID, action, allowlisted resource type,
exact run-scoped resource ID, SHA-256 of the canonical provider request and
timestamp before the provider call. Its result records a bounded outcome,
optional provider operation name and SHA-256 of the normalized response summary;
raw provider responses are never stored. A pending intent cannot be followed by
another attempt for the same target. Repeated intent/result writes reconcile the
same deterministic record after a lost ledger response. A `progress` event
records the stable provider operation name and a normalized response fingerprint
when GCP accepts an asynchronous request; it leaves the intent unresolved for
later polling. Only a terminal provider result closes the intent. Fake-provider tests
model a create that commits while its response is lost, lease takeover and
read-only lookup by the exact intended ID; they assert no duplicate create.
The original operation ID and authorization phase remain addressable if a
takeover first marks the run `incomplete`; replay returns the pending record
for reconciliation and does not authorize another provider call.
The journal is still an adapter contract: it does not call GCP, fence an
in-flight request or prove real provider discovery semantics.
`RunLedger.assert_active_lease` re-reads the exact object and checks owner,
expiry and generation for the caller immediately before it sends a provider
request. This narrows the race window; an API request already sent cannot be
cancelled or fenced by the ledger.
`scripts/recovery_controller.py` now coordinates that ledger contract with an
injected adapter: it persists and submits a new intent once, polls a journaled
long-running operation on retry, or inspects the exact resource ID after an
ambiguous submission. It never resubmits an existing intent. A create that is
still absent remains unresolved because absence alone cannot prove the original
request will not complete. Fake-adapter tests cover accepted asynchronous
operations, commit-then-lost responses, unresolved absence and lease loss before
submission. The controller has no real GCP adapter. It also has no independent
discovery/audit or deployed reaper. Managed-resource
operations must match an exact predeclared type and ID in the run inventory;
terminal create/delete results and inventory status updates share one ledger
replacement. Its cleanup method switches teardown into `cleaning`, follows the
reverse-dependency plan, resumes pending deletes and stops on unresolved or
failed results; this remains an injected-adapter contract. New create intents
are rejected at or after the fixed run deadline, while already-persisted intents
remain reconcilable. Each provider target has at most three attempts; exhausted
cleanup transitions to `incomplete` and preserves its recorded failure.
The current Terraform roots use local backends at
`bootstrap/terraform/.terraform/ephemeral-bootstrap.tfstate` and
`infrastructure/lab/.terraform/ephemeral-lab.tfstate`; those are not durable and
are not backed up by GitHub Actions. The candidate replaces lab state with an
encrypted, uniform-access GCS state bucket dedicated to that run in the
persistent recovery project. Control-plane state uses a different bucket and
owner. Enable object versioning, retention and a separately administered
private backup before the first apply; test restore of
both current and previous state generations. The Terraform execution identity
receives access only to the matching run's bucket; the reaper receives no
state-bucket access. State can be recovered only while the recovery project's
IAM and GCS control plane are available; total recovery-project loss requires
the independent backup operator and copy. The ledger remains separate and
contains no secret, kubeconfig, Terraform state, plan, raw log or profile binary.

Schema v2 now carries a `terraform_state` record with exact backend and backup
bucket names, the same run-scoped state object path, observed object generation
slots, and append-only restore-proof entries. Each proof identifies whether the
current or previous snapshot was restored, its source/backup/restored object
generations, verification timestamp, outcome and the three matching GCS CRC32C
checksums. The validator rejects shared backend/backup bucket names, non-run
object paths, malformed generation/checksum values and mismatched checksums on
a passed proof. The ledger also makes bucket/object identity immutable and
restore evidence append-only. This is contract enforcement only: it neither
provisions or reads buckets nor proves separate administrators, retention,
versioning, encryption, backup delivery or a successful real restore. Those
remain required owner review and live qualification gates.
The syntax check intentionally excludes dotted bucket names; Google also
requires bucket names to be globally unique and rejects reserved `goog`/`google`
names ([Cloud Storage bucket naming requirements](https://docs.cloud.google.com/storage/docs/buckets)).

Required manifest fields:

- Schema/version, random run ID, dedicated project ID, London region and allowed
  zones; a sorted exact `scope.enabled_api_services` allowlist; fixed
  creation/deadline timestamps and maximum lifetime.
- `finance.billing_account_id` is the exact canonical account name, alongside
  the GBP credit, estimate, alert threshold and verified notification state.
- Exact Terraform source revision, image digest, plan checksum, Config Sync/image
  tool versions and reviewed intent checksum.
- `terraform_state`: distinct exact backend and backup bucket/object references,
  observed object generations and append-only current/previous restore proofs;
  separate project ownership and real backup/restore qualification remain gates.
- State, state generation, lease owner/expiry, last heartbeat and monotonic event
  sequence. Schema v2 starts at `control_bootstrapping`, then records `prepared`,
  `project_create_requested`, `project_created`, `billing_link_requested`,
  `project_linked`, `api_enablement`, `provisioning`, `ready`,
  `teardown_requested`, `cleaning`, `auditing`, `project_retiring`,
  `controls_reclaiming`, `closed`
  or `incomplete`. Create/link requests are persisted before calling GCP so lost
  responses can be reconciled by exact ID. The reaper never guesses whether a
  project was created or billed; ambiguity becomes `incomplete` and invokes the
  project/billing operator path.
- `controls`: expected per-run reaper Job, Scheduler job, execution service
  account and invocation service account, each with exact recovery-project ID,
  deterministic name and status (`expected`, `present`, `deleted`). These are
  separate from run-project `resources`; the reaper cleanup plan cannot delete
  them. New intent starts with these controls predeclared and no workload IDs.
  `prepared` requires all controls verified present and no workload resources.
- Per-resource expected type, canonical provider URI, discovery key, dependency
  edges, ownership evidence, first-seen/last-seen time, delete attempts/results,
  and independent audit result. `audit_receipts` is append-only: each workload
  or closure receipt records the separately named auditor, observed timestamp,
  exact pre-write manifest generation, phase-specific checks, and SHA-256
  fingerprints for the discovered inventory and an external evidence artifact.
  The compact `independent_audit` field must match the latest receipt. Workload
  audit and final project/billing/control closure are separate receipts; a
  closure receipt cannot precede a passed workload receipt. Never store raw
  provider listings, bearer credentials or sensitive Terraform values in the
  manifest. The ledger rejects receipt edits and freezes the manifest after
  `closed`; only a separately authenticated adapter can establish that the
  named auditor lease belongs to a genuinely independent identity. A receipt
  write retried after response loss must reuse its timestamp, inventory and
  evidence fingerprints, and checks; the ledger reconciles the matching audit
  observation without appending a duplicate.
- `scripts/independent_auditor.py` defines the normalized read-only report for
  workload absence checks and binds the evidence to a fingerprint of the exact
  manifest snapshot it inspected. A complete report must cover Compute
  addresses, backend services, disks, firewalls, forwarding rules, health checks,
  instances, instance groups, networks, routes, router NATs, routers, subnetworks,
  target instances and target pools;
  GKE clusters and node pools; GKE Hub feature memberships, features and
  memberships; Pub/Sub subscriptions and topics; and Storage buckets, objects
  and object versions. Every surface includes pagination completeness, count
  and response fingerprint. The assessor fingerprints normalized evidence,
  fails on any observed resource or any manifest item not marked deleted, and
  returns `incomplete` when a required surface is absent or only partially
  queried. `scripts/gcp_inventory_adapter.py` now implements read-only paginated
  REST collection for those 25 workload surfaces. It requires an explicit
  bearer token and injected GET-only transport, aborts on non-200, malformed,
  warning, unreachable, or repeated-token responses, and never discovers
  credentials or mutates resources. Mocked transport tests cover pagination,
  aggregated Compute scopes, collection keys, derived NATs, Storage versions,
  and failure refusal. It accepts Compute's documented `NO_RESULTS_ON_PAGE`
  empty-scope warning and rejects other warning codes. This is source-level
  adapter coverage only: no live GCP call has qualified API behavior, IAM
  grants, API enablement, or pagination under real inventory. The service-agent
  and API inventories and project/billing/budget closure now have normalized
  report contracts and mocked read-only adapters, but still need live API and
  permission qualification. `scripts/gcp_token_identity.py` calls Google's
  standard OpenID Connect UserInfo endpoint and requires a verified expected
  email plus the stable Google `sub`. The closure assessor records the subject
  in the audit receipt, retains the verified email in the evidence report, and
  rejects the per-run reaper/scheduler identities. Only human Google user tokens
  with the `openid` and `email` scopes are supported; workload, service-account,
  and federated identities fail closed. This is source/mock qualification only;
  it does not provision or independently authorize an auditor account or bind a
  Cloud IAM/API caller to the ledger lease. Exact budget ownership reconciliation
  and cleanup mutations also remain open. `register_discovered_workload_resources()`
  now requires complete inventory, applies a narrow run-ownership check, and
  appends supported discoveries with an auditor lease and evidence receipt.
  Cleanup requires a receipt whose operation count matches the current ledger;
  after a failed or ambiguous delete the reaper releases its lease, a human
  auditor refreshes inventory, and the reaper reacquires its lease before
  resuming. Unsupported or unowned findings are fingerprinted but excluded
  from deletion and must still be absent for the final audit to pass. This is
  fake-tested source only: the GCS writer verifies an explicit token source
  against the expected email and stable subject before each request, but the
  collector, ledger and mutation adapter are connected by a source-level
  reclaimer factory, but no credential-source launcher or deployed runtime
  qualifies that composition. See Google's
  [OpenID Connect UserInfo endpoint](https://developers.google.com/identity/openid-connect/reference#userinfo)
  and [token types](https://docs.cloud.google.com/docs/authentication/token-types).
  `scripts/gcp_project_lifecycle_adapter.py` supports exact-ID project creation
  under one configured folder or organization, billing link/unlink for one
  configured billing account, API enablement from the manifest allowlist, and
  project deletion. Teardown enters `project_retiring` only after the passed
  workload audit and empty workload inventory. The ledger requires a successful
  billing unlink before it accepts a project-delete intent. Either the manual
  `operator:project-reclaimer` lease with its verified human user or the exact
  per-run `janitor:google-service-account-{numeric-sub}:{execution-id}` lease
  may record those operations. The stable principal ID is retained on the
  operation intent, and the same identity check runs on every project-retirement
  API call. Project deletion stays
  `running` while the exact project is `DELETE_REQUESTED` and resolves only on
  exact 404. The transition to `controls_reclaiming` requires resolved billing
  unlink and project deletion; `closed` still requires the independent full
  closure receipt. Fake transport tests cover target/parent/account/API
  rejection, deletion polling and the requested-versus-absent distinction.
  The v3 Project schema uses `displayName` and `state`; the Billing API request
  sends only writable `billingAccountName`, omitting output-only fields.
  The adapter is not deployed or wired to a trusted launcher, and does not solve
  creator Owner grants, billing-budget preflight, or a credential-source
  launcher/deployed project-reclaimer runtime. It remains unqualified for live
  use. Google
  documents project creation as a separate asynchronous operation
  from billing association; billing association can enable charges. See the
  [projects.create API](https://docs.cloud.google.com/resource-manager/reference/rest/v3/projects/create),
  [v3 Project schema](https://docs.cloud.google.com/resource-manager/reference/rest/v3/projects),
  [billing association API](https://docs.cloud.google.com/billing/docs/reference/rest/v1/projects/updateBillingInfo),
  [ProjectBillingInfo schema](https://docs.cloud.google.com/billing/docs/reference/rest/v1/ProjectBillingInfo),
  [Service Usage enable API](https://docs.cloud.google.com/service-usage/docs/reference/rest/v1/services/enable),
  and [project deletion API](https://docs.cloud.google.com/resource-manager/reference/rest/v3/projects/delete).
  `scripts/closure_auditor.py` now defines a separate credential-free closure
  report bound to the exact manifest. It requires complete pre-deletion
  Service Usage enabled-API and run-project service-account inventories, then
  complete project, billing, run-project budget, Cloud Run, Scheduler and
  recovery service-account coverage. The pre-deletion enabled API list must
  exactly match the sorted `scope.enabled_api_services` policy in the run
  manifest; unexpected API enablement fails closure. The project number binds
  Service Usage to the correct project, and the full service-agent list is
  fingerprinted before the project is removed. It records both sides of the
  expected billing unlink, checks exact manifest control IDs, and requires the
  earlier workload receipt to have passed. The manifest now requires the
  canonical billing account name. The budget query is scoped to
  `projects/{run_project_id}` so a complete empty result proves no project-scoped
  budgets remain; the separately existing bootstrap-project budget is outside
  this run-project scope and is not treated as run-owned. This is a normalized
  `scripts/gcp_closure_adapter.py` now collects that contract through an
  explicit-token, GET-only transport: Resource Manager project lookup, both
  retained billing-info snapshots, the project-scoped Billing Budgets list,
  Cloud Run Jobs (including unexpired deleted records), Cloud Scheduler jobs,
  and recovery-project service accounts. It fails on non-200 responses except
  the expected absent-project 404, malformed resources, warnings, or partial
  pagination. Mock transport tests prove request policy and normalization only.
  No live API adapter behavior, IAM, human principal verification, or
  budget-object ownership reconciliation is qualified. IAM's service-account list proves
  there are no active listed identities; deletion remains recoverable for up
  to 30 days, so retain the exact delete receipt and record that provider
  recovery window rather than claiming the tombstone was immediately purged.
  See Google's
  [project billing info](https://docs.cloud.google.com/billing/docs/reference/rest/v1/projects/getBillingInfo)
  and [budget list scope](https://docs.cloud.google.com/billing/docs/reference/budget/rest/v1/billingAccounts.budgets/list),
  [enabled services list](https://docs.cloud.google.com/service-usage/docs/reference/rest/v1/services/list),
  [Cloud Run Jobs list](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/list),
  [Scheduler jobs list](https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs/list),
  [service accounts list](https://docs.cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/list),
  and [service-account deletion recovery](https://docs.cloud.google.com/iam/docs/service-accounts-delete-undelete)
  references.
  The REST resource/response mapping follows Google's primary references for
  [Compute aggregated lists](https://docs.cloud.google.com/compute/docs/reference/rest/v1/addresses/aggregatedList),
  including [backend services](https://docs.cloud.google.com/compute/docs/reference/rest/v1/backendServices/aggregatedList),
  [forwarding rules](https://docs.cloud.google.com/compute/docs/reference/rest/v1/forwardingRules/aggregatedList),
  [health checks](https://docs.cloud.google.com/compute/docs/reference/rest/v1/healthChecks/aggregatedList),
  [instance groups](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instanceGroups/aggregatedList),
  [target instances](https://docs.cloud.google.com/compute/docs/reference/rest/v1/targetInstances/aggregatedList)
  and
  [target pools](https://docs.cloud.google.com/compute/docs/reference/rest/v1/targetPools/aggregatedList),
  [GKE clusters](https://docs.cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/list)
  and
  [node pools](https://docs.cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/list),
  [project-visible Fleet locations](https://docs.cloud.google.com/kubernetes-engine/fleet-management/docs/reference/rest/v1beta/projects.locations/list),
  [Fleet memberships](https://docs.cloud.google.com/kubernetes-engine/fleet-management/docs/reference/rest/v1/projects.locations.memberships/list),
  [Fleet features](https://docs.cloud.google.com/kubernetes-engine/fleet-management/docs/reference/rest/v1/projects.locations.features/list),
  [membership features](https://docs.cloud.google.com/kubernetes-engine/fleet-management/docs/reference/rest/v2/projects.locations.memberships.features/list),
  [Pub/Sub topics](https://docs.cloud.google.com/pubsub/docs/reference/rest/v1/projects.topics/list)
  and
  [subscriptions](https://docs.cloud.google.com/pubsub/docs/reference/rest/v1/projects.subscriptions/list),
  [Storage buckets](https://docs.cloud.google.com/storage/docs/json_api/v1/buckets/list)
  and
  [object versions](https://docs.cloud.google.com/storage/docs/json_api/v1/objects/list).
- Gross cost estimate, measured gross usage, currency, credit balance/expiry
  observation time, net-cash estimate and later billing reconciliation reference.
- Immutable links/checksums to sanitized evidence and an explicit limitation list.

Creation order: validate intent and exact identity -> write a create-only
`control_bootstrapping` manifest with deterministic expected control/project/
budget IDs and fixed expiry -> create the per-run reaper Job, execution identity,
Scheduler job and invocation identity in the recovery project, recording each
request and response -> verify the fixed OAuth target and Job-level invoker
binding -> transition to `prepared` and enable the five-minute schedule ->
persist `project_create_requested`, then create the disposable project by its
fixed ID -> persist `billing_link_requested`, attach billing and verify the
association -> attach/test its alerts-only budget -> enable required APIs and
record service-agent side effects -> create billable workload resources and
transition through `provisioning` to `ready`. The Job treats `prepared` as no
run project yet; it may only verify no exact project exists and must not create
or delete one. If expiry occurs while still `prepared`, it records the empty
run-project audit and alerts the operator; the record remains open until that
operator removes the scheduled trigger, Job and identities and the independent
auditor records their absence. For a lost create/link response, the Job performs
read-only exact-ID reconciliation; if billing or project state is ambiguous, it
marks `incomplete` and alerts the separately authorized operator. No workload API is enabled until project creation, billing, budget ownership
and expiry control are verified. Record each request before the provider call and response after
it. Never reset the deadline or extend expiry in place; a new attempt gets a new
run ID and separately reviewed budget.

The reaper acquires a short manifest lease using generation preconditions, reads
the immutable project/region/ID scope, and ignores closed runs. At deadline it
atomically moves any project-created or workload state to `teardown_requested`,
performs the documented reverse-dependency cleanup, records each response, and
re-lists exact resource types until absence is independently established. The
separate operator then reconciles the billing budget, removes the recovery
controls, unlinks billing and deletes the empty run project after delayed billing
review. `closed` requires a complete independent audit of resources, project,
billing, budget and recovery controls. Any permission error, ambiguous
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
| Runner killed before first recovery control | `control_bootstrapping` manifest lets the human operator reconcile partial controls; no run project or workload API may be created until trigger verification |
| Expiry fires while run is only `prepared` | Reaper confirms no project-create request and no project at the exact ID, records empty run-project audit and alerts; operator removes recovery controls and independent auditor closes |
| Runner dies after create succeeds but before ID write | Discover exact run-labeled/deterministically named resource; record then delete; if lookup is ambiguous, halt as incomplete |
| Scheduler invocation or Cloud Run execution fails | Retry boundedly, preserve run; next interval resumes from recorded state; notify failure without closing |
| Duplicate/concurrent reaper execution | Each Cloud Run execution has a unique lease owner; one CAS lease wins while active, stale generations cannot mutate the manifest, and idempotent delete tolerates already-absent owned resources. Lease expiry does not cancel a request already sent to Google Cloud; the next invocation reconciles its exact operation intent before retrying. |
| Runner loses local Terraform state | Reaper cleans exact manifest-discovered resources without reading state; owner restores state from the private versioned bucket/independent backup; retain the run as incomplete until restored state and inventory reconcile |
| Terraform destroy partially fails | Preserve state and failures; retry exact dependencies; never remove finalizers or broad-delete project resources |
| Required API/IAM permission missing | Fail preflight before first billable resource when possible; mid-run mark incomplete, notify, retain recovery path |
| Wrong project, region, run ID, deadline, or unknown resource type | Reject before API mutation; tests prove no delete call |
| Cost/credit notification delivery absent | Preflight fails before workload provisioning; teardown notices are also checked |
| Reaper/ledger unavailable | No new run starts; human recovery follows private runbook; mark safety limitation, do not claim automatic cleanup |
| GKE deletion protection on | Terraform's state-side `deletion_protection` blocks Terraform destroy while true; it does not block a direct GKE REST delete. The API cleanup path therefore requires an exact-cluster review gate and performs normal DELETE without a force flag. A Terraform destroy path must first set the argument false and apply that change to state. |
| Audit API is eventually consistent | Require authoritative service-specific get/list checks after deletion; Cloud Asset Inventory is supplementary |

## CP-1B implementation packet

File-scoped implementation after CP-1A review; no cloud apply in CP-1B:

1. `scripts/run_contract.py` and its fixtures/tests: schema v3 separates
   recovery/workload projects and reaper identities, binds the ledger bucket,
   project parent and reaper identity audience, predeclares the per-run
   controls, models project/billing phases and audits controls separately from
   workload IDs. The contract carries distinct state/backup bucket identifiers,
   generation references and append-only restore-proof fields. Reject
   cross-project IDs, ambiguous create/link results and
   unauthorized project transitions. `scripts/run_contract_cli.py` emits only
   exact-ID dry-run plans. `scripts/run_ledger.py` implements create-only and
   generation-conditional exact-path manifest storage, bounded event history,
   and CAS leases with a fake-store suite. The manifest now records exact
   backend/backup paths, append-only current/previous restore proof metadata,
   and durable phase-bound provider intents with deterministic IDs, progress
   receipts for accepted long-running operations, and terminal results. The
   credential-free `scripts/recovery_controller.py` submits or reconciles a
   operation or performs reverse-dependency cleanup through an injected
   adapter. Managed workload operations are bound to exact predeclared
   inventory entries, and terminal results atomically update resource status.
   Fake tests cover response loss, long-running polling and lease loss. Fake
   create-response-loss takeover across a phase change is covered. Bucket
   isolation, backup administration, versioning/retention, independent audit,
   real provider reconciliation, adapters for the full resource matrix and
   actual restore remain unqualified.
2. `bootstrap/terraform/`: now owns only persistent bootstrap-project APIs,
   GitHub federation, cost-alert scaffolding and lifecycle alert policies; it
   no longer owns run APIs, node identity or the Config Sync Feature. Still
   separate the existing GBP budget/channel from the new recovery-project
   controls and reconcile ownership before any apply.
3. `infrastructure/lab/`: now owns run-only APIs, `configmanagement` Feature,
   node service account/binding, VPC, GKE, Fleet membership and membership
   feature in the disposable run project. Add an explicit run inventory for node pools,
   service agents and provider-generated resources. Keep deletion protection on
   by default; retain Terraform state through cleanup. A REST delete bypasses
   the provider's state-side guard and therefore requires the exact-cluster
   review gate; a Terraform destroy path must apply `deletion_protection=false`
   to state first.
4. `bootstrap/terraform/backend.tf`, `infrastructure/lab/backend.tf`, and the
   controller init path: replace local state with the selected private GCS
   backend, create the per-run state bucket before `terraform init`, grant only
   the Terraform identity access to that bucket, and verify backup/restore.
   The reaper never reads state.
5. New recovery-controller modules plus `scripts/run_contract.py` adapters:
   connect the create-only ledger record and all per-run recovery controls before
   project creation; finish provider discovery, independent absence audit and
   provider reads/deletes; bind reaper writes to the ledger lease; separate
   project/billing, audit and control-reclamation roles. Ordered cleanup now
   resumes pending operations. Append-only generation-bound audit receipts are
   now part of the ledger contract, but external inventory discovery, billing/
   project checks, independently administered auditor identity, runtime wiring
   of the principal-bound ledger client, production wiring, and full resource-matrix adapter
   coverage remain open. The
   ledger fake covers response-loss retries and CAS conflicts; extend
   fake-provider coverage now includes a create committed before a lost response,
   exact-ID reconciliation, idempotent cleanup after confirmed deletion, and
   rejection of another run's project ID before any provider call. This is an
   application/ledger contract, not proof of Google IAM cross-run denial. Fake
   cleanup also covers provider deletion followed by a lost response: takeover
   inspects the exact ID, records absence and does not submit a second delete.
   `scripts/gcp_cleanup_adapter.py` adds a deletion-only REST adapter for
   Compute network/subnetwork/router/NAT, Pub/Sub topics/subscriptions, exact
   GCS object versions, GKE node-pool/cluster, and Fleet membership/feature
   resources. Its injectable transport and explicit short-lived token source
   have local mock tests for target paths, URL and project/run boundaries,
   long-running operation scope, router fingerprints, storage generation
   preconditions, bucket project ownership, and the exact-cluster review gate.
   It cannot create resources. `gcp_inventory_adapter.py` now emits the v3 report
   project-bound bucket/object/generation ID form and inventories soft-deleted
   generations separately when a bucket reports active retention. Cloud Storage
   defaults new buckets to seven-day soft deletion; Terraform must explicitly
   create ephemeral buckets with zero retention. The mutation adapter rejects
   active retention, and the API cannot immediately purge already soft-deleted
   objects; those leave cleanup incomplete until retention expires. The adapters
   are not wired to `RecoveryController`, deployed identities or live providers.
   The cluster gate is only an external predicate; it does not inspect Terraform
   state. The Google provider's `deletion_protection` is a Terraform-side guard,
   not an API setting; the REST adapter's review gate is the deletion control on
   that path. Dynamic inventory registration now has a bounded ledger contract
   and fake-tested auditor handoff. Trusted runtime auditor/API principal
   binding, actual IAM
   isolation, remote state/backup and lifecycle deployment remain open.
6. `.github/workflows/validate.yaml` remains credential-free for CI and runs
   schema, Terraform-render, cleanup, state, and negative-boundary checks. Add
   lifecycle workflow only after the reaper and cleanup path are deployed and
   qualified; validate source, state, identity, costs and budget notification
   before any billable object, and make cancellation recoverable.

**CP-1B completion gate:** positive and negative tests, hosted credential-free
validation, exact allowed files and plan, documented API/role matrix, fake
runner-loss and idempotency drills, and a reviewed CP-1C live drill packet.

The Python adapter uses documented `if_generation_match` conditions for GCS
uploads and downloads. See [Google's Python client reference](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.blob.Blob)
and [Cloud Storage request-precondition guide](https://docs.cloud.google.com/storage/docs/request-preconditions).
