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
4. Verify the Job, trigger, runtime identity, exact ledger IAM condition, and
   exact Job invocation grant against the manifest. Do not continue to project
   creation unless the trigger is active and the image runs the reviewed
   entrypoint.
5. After the exact disposable project exists, review support for all three
   project-level custom-role permissions in the target organization. Only then
   apply `enable_project_retirement_iam = true` with the same remote state.
6. Keep the recurring trigger and private state through workload cleanup,
   independent closure audit, and billing reconciliation. The Job has deletion
   protection; control reclamation is a separate reviewed post-audit action.

The run-project provider alias has no managed resources while the retirement
IAM flag is false. No workload IAM, project creation, billing attachment,
control reclamation, or state-bucket provisioning is part of this root. The
Terraform CI tests use mocked providers and never apply cloud resources.
