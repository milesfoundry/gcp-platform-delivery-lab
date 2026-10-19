# Validation and activation runbook

## Current safety design

[GCP safety and recovery design](safety-recovery-design.md) defines the current
CP-1 ownership matrix, run manifest, state transitions, cleanup order, failure
cases and next implementation packet. The validator in
`scripts/run_contract.py` and generation-CAS storage adapter in
`scripts/run_ledger.py` are credential-free source components. The adapter has
fake-store tests for response-loss retries, leases, heartbeats, and stale-writer
rejection but no configured bucket, cloud identity, deployed ledger,
Cloud Run reaper, provider adapter, cleanup controller, janitor or cloud
qualification. The manifest records deterministic provider-operation intents
and results, including the lifecycle phase that authorized each intent; fake
provider tests cover a committed create with a lost response and exact-ID
reconciliation after lease takeover. No cloud resource creation is authorized
by these files.

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
   and test each run project's cost-alert route before provisioning.
2. Complete and qualify the [separate identity/alert bootstrap](cloud-bootstrap.md),
   recovery controls and private state/backup path. Terraform state is currently
   local and is not runner-loss safe; never upload it as an artifact/cache or
   commit it. Never commit plans, service-account keys or environment credentials.
3. Qualify the selected GKE/Config Sync versions and fleet RBAC behavior.
4. Qualify the exact janitor IAM boundary. Schema v2 and the dry-run planner
   separate recovery controls and workload inventory, but do not call Google
   APIs or prove cross-run IAM denial. No reaper may be enabled until project
   creation, lowest-grant scopes, provider calls and negative cross-run tests pass.
5. Configure and qualify the generation-conditional GCS ledger, then implement
   the GCP provider adapter, operation controller and out-of-band reaper. Extend
   fake-provider drills to delayed responses, create/delete loss and independent
   inventory before a separately approved bounded cloud drill. Use the private
   remote state and restore procedure, not local job state. Cluster deletion
   protection must be changed explicitly for an approved teardown.
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
