# GCP state and federated CI bootstrap

The bootstrap Terraform root is separate from the disposable lab root. It declares
GitHub federation, a CI service account, project budget/lifecycle alerts and
explicitly reviewed project roles. Terraform state is local to a trusted operator
or short-lived run workspace. This configuration is validated without credentials;
activation waits for the chosen project, region, budget/lifetime and access setup.

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

Bootstrap owns IAM/federation/budget/alert APIs, including the shared Logging and
Monitoring API enablement. The lab root owns GKE/network/fleet/configuration APIs
and its Storage/Pub/Sub enablement. Both roots use local state files under their
own `.terraform/` directories. This avoids a persistent GCS backend, but means an
independent provider-side run inventory/janitor is mandatory if the workflow
runner disappears. Never upload either root's state as an Actions artifact/cache.
Preserve state until independent provider inventory confirms deletion; then remove
the local workspace/state.

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
3. Create the reviewed federation/budget/alert resources; record identifiers
   privately.
4. Confirm both roots use local state and neither uploads state to artifacts/cache.
5. Review the minimum project role set and dedicated lifecycle workflow.
6. Test budget and lifecycle notification delivery plus token acceptance/rejection
   for allowed/forbidden workflow contexts.
7. Only then plan a bounded lab create/run/export/delete cycle; preserve state until
   independent resource inventory confirms deletion.

Provider validation does not prove claim evaluation, cloud permissions, local-state
recovery, alert delivery or the lifecycle. Those require actual qualification. The
lifecycle event publisher and six event-specific log alert policies are implemented,
but are not wired to a run workflow. The
independent expiry janitor is still absent. No cloud
apply workflow or cloud resources have been activated by this change.
