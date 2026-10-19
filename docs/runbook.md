# Validation and activation runbook

## Current safety design

[GCP safety and recovery design](safety-recovery-design.md) defines the current
CP-1 ownership matrix, run manifest, state transitions, cleanup order, failure
cases and next implementation packet. The validator in
`scripts/run_contract.py` and generation-CAS storage adapter in
`scripts/run_ledger.py` plus normalized evidence assessor in
`scripts/independent_auditor.py` are credential-free source components. The
ledger adapter has fake-store tests for response-loss retries, leases,
heartbeats, and stale-writer rejection but no configured bucket, cloud
identity, deployed ledger, Cloud Run reaper, workload cleanup provider adapter,
independently administered external auditor, janitor or cloud qualification. The
read-only `gcp_inventory_adapter.py` collects the 25 workload surfaces using
an explicitly supplied bearer token and injected GET-only transport.
`gcp_closure_adapter.py` collects project, billing, API, service-account,
budget and recovery-control closure. Both call Google's standard OpenID
Connect UserInfo endpoint and require its verified email to match the expected
auditor email supplied by the caller. Receipts identify the user by Google's
stable `sub`; the email is retained in the normalized report. The auditor must
differ from the per-run reaper and scheduler identities. Only human Google user
tokens with the required `openid` and `email` scopes are supported; other
identities fail closed. Mocked tests do not establish live IAM, API enablement,
provider behavior or an independently administered auditor account. The manifest records deterministic provider-operation intents
and results, including the lifecycle phase that authorized each intent; fake
provider tests cover a committed create with a lost response and exact-ID
reconciliation after lease takeover. Append-only progress receipts retain the
provider operation name for accepted asynchronous requests without marking them
complete; only a terminal result resolves an intent. The credential-free
`recovery_controller.py` submits once or resumes by polling/inspecting through
an injected provider adapter, and can run ordered cleanup against that adapter.
The project lifecycle adapter also supports the separate post-audit project
reclaimer: billing must be unlinked first, project deletion is journaled, and the
operation remains unresolved while Resource Manager reports `DELETE_REQUESTED`.
Recovery controls can be reclaimed only after exact project absence is
reconciled. The controller requires the named reclaimer lease, verifies the
Google user token against a configured expected email, and records the stable
Google subject with each retirement intent. Each retirement API request is
verified before use. The GCS writer requires an explicit token source, expected
recovery project ID, email and stable subject, and verifies them before each
storage request. The `project_reclaimer_runtime.py` factory passes the same
token source and identity transport to both adapters and the controller. The
credential-source launcher and deployed runtime remain unqualified; this path
is fake-tested only.
`gcp_cleanup_adapter.py` now implements a deletion-only adapter for exact
Compute network/subnetwork/router/NAT, Pub/Sub topics/subscriptions, GCS objects,
GKE node-pool/cluster, and GKE Fleet membership/feature resources. It requires an
explicitly supplied short-lived token source and transport; it does not discover
credentials. Mocked tests cover request paths, project/run boundaries,
async-operation scope, router fingerprint updates, and the cluster-protection
review gate. GCS object IDs bind project, bucket, encoded object name and either
an exact generation or `live`; deletion checks project number, bucket ownership,
London location and soft-delete policy. The inventory adapter lists soft-deleted
generations separately when bucket retention is active. A bucket with active
soft-delete retention cannot be cleaned within the lab deadline and is rejected
by the mutation adapter. The cleanup adapter has not been wired to the ledger
controller or deployed identities. Its cluster review callback is an external
gate, not proof of Terraform state or a GKE API deletion-protection setting.
The source now registers supported run-owned discoveries under a separate
auditor lease before cleanup. The GCS writer verifies an explicit token source
against the configured Google email and stable subject before each request;
runtime assembly is now source-tested, while credential sourcing, IAM isolation
and live provider behavior remain unqualified. GCS deletion verifies
the project number, bucket ownership and London bucket location; it requires an
explicit zero-duration soft-delete policy and rejects buckets with active soft
delete retention. `gcp_project_lifecycle_adapter.py` now provides source-level
exact-run project creation, reviewed billing association/removal, API enablement
for the manifest's sorted allowlist, and post-audit project deletion. It uses an
explicit short-lived token source and HTTPS transport, with fake tests for exact
IDs, parent, run labels, billing account and operation scope. Retirement
requires a verified human principal. It is not wired to a deployed launcher or
live GCP identity. Its presence does not authorize cloud activation; no cloud
API call is made by CI or this runbook.
Fake controller tests cover a create committed before a lost response,
cross-run project-ID rejection before provider access, and repeated cleanup
after a confirmed delete without a second delete call. These verify the local
ledger/controller contract only; they do not establish Google IAM isolation or
provider behavior.

Workload provider operations must target an exact `(type, id)` already declared
in the run manifest inventory. A terminal successful create or delete result and
the corresponding `expected/present/deleted` inventory status are written in
one generation-CAS replacement. Failed or unresolved operations leave inventory
status unchanged. `RecoveryController.cleanup()` switches a requested teardown
to `cleaning`, follows the manifest's reverse dependency order, resumes the same
pending delete after takeover, and stops on the first failed, running or
unresolved operation. A run cannot start a new create at or after its fixed
deadline; an already-persisted create remains reconcilable. Provider attempts
are capped at three, after which cleanup records `incomplete`. Fake controller
tests cover create and delete response loss, cross-run project-ID rejection
before provider access, and repeated cleanup after a confirmed delete without a
second delete call. These verify the local ledger/controller contract only;
they do not establish Google IAM isolation or real provider behavior.
Before every cleanup attempt, the reaper enters `cleaning` and releases its
lease. The independently authenticated human auditor collects a complete
inventory and appends only supported resources whose exact IDs prove run
ownership. Unsupported or ambiguous findings are fingerprinted in the receipt
but never become delete targets; the final absence audit must still account for
them. The reaper then reacquires its lease and may clean only after a discovery
receipt matches the current provider-operation count. A failed or ambiguous
provider operation requires a fresh inventory before resuming cleanup. This
handoff is implemented and fake-tested in source, but the collector, ledger and
mutation adapter are not wired together in a deployed identity or live GCP
environment. The source-level reclaimer factory wires one token source and
identity transport through GCS and provider requests; credential sourcing,
control reclamation and live provider qualification remain open.

The 2026-10-18 IAM review found that the current shared lab-project layout
does not yet prove a reaper can delete its run's Compute network resources
without gaining broader project-level access. The working design candidate is
one disposable project per run, with the ledger and generic reaper in a
separate persistent recovery project; this boundary is not implemented or
approved for live activation. See the IAM finding in the safety design before
changing Terraform or planning a cloud run.

Use `python3 scripts/run_contract_cli.py intent|manifest|cleanup|controls
<record.json>` to validate a local record or print an exact-ID dry-run plan.
The CLI has no provider client or delete path; `controls` refuses to plan reaper
removal until the workload inventory has passed its independent audit.

## Ephemeral HA cloud execution policy

Follow the [ephemeral HA architecture and cost policy](architecture-cost.md)
before activation. London multi-zone HA, 4 GiB minimum-first nodes, layered
provider-specific alerts, £5 gross per run / £10 for the initial pair as planning
reference amounts, 60-minute maximum lifetime and immediate audited cleanup are
the approved starting policy. Cost tracking protects the free-credit runway;
these amounts are not provider-guaranteed caps. Verify current credit balance and
expiry, cost estimate, alert delivery and cleanup before apply. Existing
topology/intent validators do not yet enforce the complete policy.

## Credential-free checks

Use Go 1.27.1, Terraform 1.16.4, kubectl with Kustomize, Python 3 and PyYAML 6.0.2.
The provider lock contains Linux amd64 and arm64 checksums.

```sh
cd workload
go test ./...
go vet ./...
# Integration tests skip unless TEST_DATABASE_URL is set.
# Use ONLY a disposable database: the suite truncates the jobs table.
TEST_DATABASE_URL='postgres://workshop:local-demo-only@127.0.0.1:5432/workshop?sslmode=disable' go test -race ./...
cd ..
terraform fmt -check -recursive infrastructure bootstrap
python3 -m unittest discover -s scripts -p 'test_*.py' -v
terraform -chdir=bootstrap/terraform init -input=false -lockfile=readonly
terraform -chdir=bootstrap/terraform validate
terraform -chdir=bootstrap/terraform test
terraform -chdir=infrastructure/lab init -input=false -lockfile=readonly
terraform -chdir=infrastructure/lab validate
terraform -chdir=infrastructure/lab test
python3 scripts/check-ownership.py
```

The integration URL is illustrative: start a dedicated PostgreSQL test instance
or use the hosted workflow's isolated service. The default application Compose
database intentionally has no published host port.

## Before any cloud apply

This is not yet an executable end-to-end cloud runbook. Do not run apply merely
because validate passes. Required outstanding gates:

1. Confirm the approved parent folder, one disposable run project, separate
   recovery/FinOps project, billing account, London region/zones, operator egress
   CIDR, GBP billing currency, current credit balance/expiry, £5 per-run planning
   reference and 60-minute expiry. Record gross usage, net cash and projected
   credit runway. Reconcile the existing project-filtered budget, then configure
   and test each run project's cost-alert route before provisioning. The v2 run
   manifest must include a sorted `scope.enabled_api_services` list matching
   `infrastructure/lab`'s declared API set; the independent pre-deletion
   Service Usage snapshot must match it exactly.
2. Complete and qualify the [separate identity/alert bootstrap](cloud-bootstrap.md),
   recovery controls and private state/backup path. Terraform state is currently
   local and is not runner-loss safe; never upload it as an artifact/cache or
   commit it. Never commit plans, service-account keys or environment credentials.
3. Qualify the selected GKE/Config Sync versions and fleet RBAC behavior.
4. Qualify the exact janitor IAM boundary. Schema v2 and the dry-run planner
   separate recovery controls and workload inventory, but do not call Google
   APIs or prove cross-run IAM denial. No reaper may be enabled until project
   creation, lowest-grant scopes, provider calls and negative cross-run tests pass.
5. Configure and qualify the generation-conditional GCS ledger, wire the
   deletion-only adapter into the controller, and deploy the out-of-band reaper.
   Extend provider drills to delayed responses and independent inventory before
   a separately approved bounded cloud drill. Use private remote state and its
   restore procedure, not local job state. For the REST cleanup path, retain
   Terraform state and require the adapter's separate exact-cluster review
   before deletion. The provider's `deletion_protection` flag only guards
   Terraform destroy: if using Terraform to destroy, set it false and apply
   that state change first. It is not a GKE API protection field.
6. Add application cloud IAM, Pub/Sub/GCS resources and adapters, image release
   provenance, network policies and workload deployment.

The bootstrap Terraform root includes six opt-in event-specific Monitoring log
alerts plus the [`notify_lifecycle.py`](../scripts/notify_lifecycle.py) publisher. Once
bootstrapped, emit only the allowlisted `created`, `ready`, `expiry-warning`,
`teardown-started`, `teardown-passed` and `teardown-failed` events. A scheduler or
workflow must call the 15-minute expiry warning; no scheduler or cloud-run workflow
is implemented yet. Alert creation and delivery have not been cloud-tested.
The emitting identity needs `logging.logWriter` on the configured recovery
project; do not grant broad Monitoring administration to the run workflow.

No workflow in this repo has cloud credentials or performs Terraform apply.
Budget intent is not a spending cap. A future cloud plan must list all billable
resources including NAT, disks, logging and any retained artifacts. Destroy the
run as soon as evidence capture is complete or the lab becomes idle; do not leave
billable infrastructure running between sessions.

## Local failure diagnosis

- API not ready: inspect database health and `docker compose logs migrate api`.
- Job remains running: observe lease expiry, worker logs and next attempt;
  after five expired attempts expect terminal failure.
- 409 on submit: the key was already used for different input; use a new key.
- 409 on report: the report is not yet committed; poll the job state.
- Failed migration: stop and inspect; do not erase retained data to hide failure.

The schema bootstrap uses an advisory lock and idempotent creation. It is not a
general migration engine and must be replaced before schema-changing releases.
