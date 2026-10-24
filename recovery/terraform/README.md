# Per-run recovery controls

This root manages one run's Cloud Run retirement Job, recurring Cloud Scheduler
trigger, service accounts, and conditional manifest-object access in the
persistent recovery project. It must use a private, per-run GCS state bucket
separate from the manifest ledger. Create a private `backend.hcl` from
`backend.hcl.example` and initialize with
`terraform init -backend-config=backend.hcl`; never initialize this root with
local state.

## Required ordering

1. Review the recovery project, exact target project ID, parent, billing
   account, API allowlist, independent audit path, immutable image digest, and
   private state bucket. The digest must identify the public
   `ghcr.io/OWNER/gcp-reaper` package until an Artifact Registry remote
   repository is provisioned for private GHCR access. Verify the ledger bucket
   is in the recovery project and has uniform bucket-level access enabled.
2. Validate the complete intent, then register it before applying this root:

   ```sh
   python3 scripts/run_contract_cli.py intent run-intent.json
   python3 scripts/register_gcp_run.py \
     --intent run-intent.json \
     --credentials-file /secure/path/google-user.json \
     --ledger-bucket portfolio-ledger-private \
     --ledger-project-id recovery-project-id \
     --expected-email operator@example.com \
     --expected-auditor-id google-user-12345678901234567890
   ```

   The credential file must be an explicitly supplied authorized-user
   credential kept outside the repository. The command verifies the configured
   Google user against UserInfo and writes only `runs/<run-id>/manifest.json`
   with a create-only generation precondition. A retry with the same intent
   reconciles a lost response; a changed intent for the same run ID fails.
   Confirm the returned object path and generation. The manifest must be in
   `control_bootstrapping` before any recovery control is created.
3. Apply with
   `enable_project_retirement_iam = false`; this creates the Job and recurring
   trigger before the disposable project exists.
4. Run `scripts/verify_gcp_recovery_controls.py` with the same intent,
   explicitly named operator credential, expected Google principal, ledger
   bucket, and recovery project. The read-only checks cover the enabled
   Scheduler, ready pinned-image Job, both service accounts, exact Job
   invoker binding, ledger custom role, exact-object bucket IAM condition,
   bucket project/UBLA, and absence of the disposable run project. It appends
   the generation-bound verification receipt and changes the manifest to
   `prepared` only if every check passes. Do not request project creation
   unless the command returns `state: prepared`.

   ```sh
   python3 scripts/verify_gcp_recovery_controls.py \
     --intent /secure/path/run-intent.json \
     --credentials-file /secure/path/operator.json \
     --ledger-bucket LEDGER_BUCKET \
     --ledger-project-id RECOVERY_PROJECT_ID \
     --expected-email operator@example.com \
     --expected-auditor-id google-user-USERINFO_SUBJECT
   ```
5. After the exact disposable project exists, review support for all three
   project-level custom-role permissions in the target organization. Only then
   apply `enable_project_retirement_iam = true` with the same remote state.
6. Keep the recurring trigger and private state through workload cleanup,
   independent closure audit, and billing reconciliation. The Job has deletion
   protection; control reclamation is a separate reviewed post-audit action.

The source-level activation runner is `scripts/gcp_project_bootstrap.py`. It
requires the run's `prepared` receipt, the exact project ID and registered
intent digest as confirmations, fresh operator evidence for available credit
and alert delivery, and an absent-project read immediately before the create
phase. After billing is linked, `scripts/gcp_finance_preflight.py` performs
read-only checks for the exact active project, billing account, and one
project-filtered GBP gross-cost budget with the expected thresholds and email
route. The budget must use the exact recovery-project email channel from the
run intent and suppress default, project-level, and Pub/Sub recipient routes.
The ledger records that budget receipt before it permits API enablement. If the budget is absent, the runner stops in
`budget_verification`; a cost operator must reconcile and test the run-specific
budget before resuming. The runner journals each project, billing, and API
operation before the provider call and resumes pending operations by exact ID.
These components have fake-provider qualification only: the user-credential
launcher CLI and live project activation are not qualified. Do not use them
for a live run until launcher identity, IAM, budget delivery, recovery controls,
private state/backup and the separately authorized activation path have been
reviewed and qualified.

The run-project provider alias has no managed resources while the retirement
IAM flag is false. No workload IAM, project creation, billing attachment,
control reclamation, or state-bucket provisioning is part of this root. The
Terraform CI tests use mocked providers and never apply cloud resources.
