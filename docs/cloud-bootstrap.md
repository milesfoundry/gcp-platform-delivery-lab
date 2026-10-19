# GCP state and federated CI bootstrap

The bootstrap Terraform root owns the persistent bootstrap/recovery project's
GitHub federation, CI service account, reviewed project roles and cost/lifecycle
alert scaffolding. The lab root owns each disposable run project's APIs, network,
GKE, Config Sync Feature and run-specific node identity. Both backends are still
local. CI validates these sources without credentials and never reads or applies
live state.

**Isolation status (2026-10-18):** the Terraform resource ownership split now
matches the one-project-per-run candidate, but project creation/billing,
recovery Jobs/Schedulers, durable ledger, remote state/backup, per-run budgets
and independent cleanup are not implemented. The current sources do not authorize
an apply. No state migration is required: both configured backend state files
were checked and contain zero managed resources.

## Identity contract

The provider accepts tokens for this repository's numeric repository/owner IDs,
main branch, the named cloud-run.yaml workflow, and workflow_dispatch events.
Those IDs were verified through GitHub's repository API on 2026-10-04.
The workflow file is reserved for the later cloud lifecycle implementation;
ordinary Validate/fork jobs cannot use this trust.

Project roles default to an empty set; cloud provisioning needs an explicitly
reviewed role list. Owner/Editor are rejected. No service-account key resource is
created. No retained GCS state bucket or state-writer binding is created.
Initial bootstrap still needs an authorized operator's short-lived login; GitHub
cannot federate into a provider that does not yet exist.

GitHub's current default subjects can include immutable numeric IDs for newer
repositories. The mapping uses the actual subject plus numeric attribute
conditions, rather than constructing the older name-only subject format.
AWS trust must likewise use the subject format actually emitted for its repo.

References:

- [Google deployment-pipeline federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
- [Google federation best practices](https://docs.cloud.google.com/iam/docs/best-practices-for-using-workload-identity-federation)
- [GitHub OIDC claims/immutable subjects](https://docs.github.com/en/actions/reference/security/oidc)

## State ownership

Bootstrap owns GitHub federation, its CI identity and reviewed role grants,
plus the existing project's budget/email/lifecycle alert resources and their IAM,
STS, Resource Manager, Billing Budgets, Monitoring and Logging APIs. The existing
project-filtered GBP budget remains attached to the configured bootstrap project;
it does not automatically cover disposable run projects.

The lab root owns Compute, GKE, GKE Hub, Config Management, Connect, IAM,
IAM Credentials, Resource Manager, Service Usage, Logging, Monitoring, Storage
and Pub/Sub API enablement in the run project. It creates one run-specific node
service account with only `roles/container.defaultNodeServiceAccount`, the
project-scoped `configmanagement` Feature, network, cluster, node pool and Fleet
membership resources. These API services stay enabled until separate project
reclamation. The reaper cannot change project IAM or delete the project-owned
node identity; a separate project-reclaimer unlinks billing and deletes the exact
project after the passed workload audit. Its ledger gate waits for the project
GET to return 404 before recovery controls can be reclaimed. This is
mock-tested source only: the operator lease is not bound to a verified cloud
principal and no live project-deletion path is qualified.

Both roots use local state paths under `.terraform/`. This is not runner-loss
recovery. The candidate remote state bucket, separate private backup, durable
manifest and independent janitor are still absent. Never upload either root's
state as an Actions artifact/cache. Preserve state until independent inventory
and billing audit pass; then retain it under the future state-retention policy.

The lab root's local backend path is `.terraform/ephemeral-lab.tfstate`; the
bootstrap root uses `.terraform/ephemeral-bootstrap.tfstate`. Credential-free CI
initializes these local backends for schema/tests but never applies or reads live
state. Terraform state contains sensitive resource details: never publish state,
saved plans or credential caches.

## Activation sequence

Once cloud settings are approved and access is verified:

1. Review the bootstrap plan under the operator identity.
2. Configure the approved private cost-alert recipient, enable the project budget,
   and choose a GBP notification threshold using the £5 per-run planning reference,
   current account baseline and remaining credit runway. The threshold is not a
   spending cap. Keep the five actual and two forecast thresholds.
   Budget/email inputs and Terraform state must stay private.
3. The per-run project launcher, recovery controls, state backup, budget
   delivery and independent cleanup path must be implemented and qualified before
   any lab project is created. No current Terraform path performs that lifecycle.
4. Confirm both roots use local state and neither uploads state to artifacts/cache.
5. Review the separate provisioner, reaper, project/billing and auditor roles;
   their exact scopes and the dedicated lifecycle workflow.
6. Test budget and lifecycle notification delivery plus token acceptance/rejection
   for allowed/forbidden workflow contexts.
7. Only after a separately reviewed activation decision can an operator plan a
   bounded lab create/run/export/delete cycle; preserve state until independent
   resource inventory and billing review confirm cleanup.

Provider validation does not prove claim evaluation, cloud permissions, local-state
recovery, alert delivery or the lifecycle. Those require actual qualification. The
lifecycle event publisher and six event-specific log alert policies are implemented,
but are not wired to a run workflow. The
independent expiry janitor is still absent. No cloud
apply workflow or cloud resources have been activated by this change.
