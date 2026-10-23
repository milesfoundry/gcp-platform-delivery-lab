# AGENTS.md

Root guidance for `milesfoundry/gcp-platform-delivery-lab`. Read
[the runbook](docs/runbook.md) before touching Terraform or any cloud path.

## Estate engineering foundation

The [estate constitution](https://github.com/milesfoundry/iac-platform/blob/main/CONSTITUTION.md) applies to this lab as well as the infrastructure
and agent repositories. Use its ownership/boundary, reproducibility/recovery,
evidence and bounded-review principles. Local provider contracts, commands and
runbooks below supply the implementation details. The canonical document is an
internal contributor reference and may require organization access.

Applicability does not grant CI admission, cloud credentials, spending or provider
mutation authority. TAL-221's CI-control decision is complete; TAL-194 provider
work remains open and separate. Preserve the existing gates.

## Purpose

A delivery lab, not a running cloud platform: a small deterministic asynchronous
Go application (**Report Workshop**) plus the Terraform and Config Sync sources
that would deliver it. The local application runs, Terraform validates without
credentials, and no GCP project has been provisioned by this repository.

| Area | Source |
| --- | --- |
| Go application | `workload/` (`cmd/workshop`, `internal/`) |
| Disposable cloud lab | `infrastructure/lab/` |
| Identity and budget bootstrap | `bootstrap/terraform/` |
| Config Sync / Flux delivery | `gitops/platform/`, `gitops/apps/report/` |
| Local evidence tooling | `workload/evidence/`, `scripts/` |
| Decisions and runbooks | `docs/` |

## Canonical edit locations

- Domain rules: `workload/internal/domain/`. No I/O, no provider types.
- Orchestration and outbox: `workload/internal/application/` and
  `workload/internal/postgres/`.
- Provider adapters: `workload/internal/cloudaws/`, `workload/internal/cloudgcp/`
  and the queue/outbox implementations in `internal/postgres/`. Provider SDK
  calls belong here, never in `domain` or `application`.
- HTTP surface: `workload/internal/httpapi/`. `openapi.json` is committed source
  embedded with `go:embed` and served at `/openapi.json`; `evidence/check_api.py`
  compares served against file, so change both together.
- Schema: `workload/internal/postgres/schema.sql`, applied by
  `cmd/workshop migrate`; the `schema-check` mode exists for drift.
- Composition: `workload/compose*.yaml`. `compose.replicas.yaml` and
  `compose.tracing.yaml` are additive overlays; `observability`-profile services
  (Prometheus, collector) are opt-in, which is why the database ports in the base
  file are the only default ones.

## Setup and checks

Go 1.27.1, Terraform 1.16.4, Python 3, kubectl with Kustomize. Credential-free,
from the repository root:

```sh
cd workload
gofmt -l cmd internal            # must print nothing
go vet ./...
go test -race -count=1 ./...      # integration tests skip without TEST_DATABASE_URL
cd ..
terraform fmt -check -recursive infrastructure bootstrap
terraform -chdir=bootstrap/terraform init -input=false -lockfile=readonly
terraform -chdir=bootstrap/terraform validate
terraform -chdir=bootstrap/terraform test
terraform -chdir=infrastructure/lab init -input=false -lockfile=readonly
terraform -chdir=infrastructure/lab validate
terraform -chdir=infrastructure/lab test
python3 -m unittest discover -s scripts -p 'test_*.py'
kubectl kustomize gitops/platform > /tmp/platform.yaml
kubectl kustomize gitops/apps/report > /tmp/app.yaml
python3 scripts/check-ownership.py
```

These are representative credential-free checks from the `Validate` workflow source
(the full ordered list is in `.github/workflows/validate.yaml`); they need no
credentials or cloud project. That workflow is currently disabled, but these
commands remain the local validation contract. Pass `TEST_DATABASE_URL` only with a
disposable database: the suite truncates the jobs table.

Local application, from `workload/`: `docker compose up --build -d`, then
`python3 smoke.py`, then `docker compose down`. The API binds 127.0.0.1:18080 and
the database is not published to the host.

## Paid or mutating operations are not validation

`terraform apply`, any project bootstrap or lifecycle script, `gcloud` project
creation and teardown spend credit or mutate cloud state. A passing
`validate`/`test` is not permission to apply: the gates in
[the runbook](docs/runbook.md) and the policy in
[architecture and cost](docs/architecture-cost.md) come first. Do not run these
to check a change.

## Architectural constraints

- Keep the layering: domain → application → ports → adapters.
- Delivery is outbox-driven. The job row and its outbox row commit together and
  the worker completes with a fence; keep completion fenced so a worker that lost
  its lease cannot write twice. Do not replace it with an unfenced update.
- The AWS and GCP adapters implement the same ports; keep their behaviour
  parallel.
- Cloud adapters are exercised with protocol doubles. Live cloud queue, IAM and
  object contracts are **not** demonstrated.

## Deployment and runner authority

- Workflow YAML still declares GitHub-hosted `ubuntu-24.04`, plus
  `ubuntu-24.04-arm` for the native ARM job. The estate's shared self-hosted group
  is private-only and this public repository cannot join it — do not change
  visibility or OIDC trust to work around that, and do not present hosted runners
  as a reusable fallback for other repositories.
- **TAL-221's CI-control decision is complete (23 October 2026).** The owner
  approved disabling `Validate`, and a Codex API readback confirmed the workflow is
  `disabled_manually`. The YAML retains its `push`/`pull_request` declarations and
  hosted labels, but the disabled workflow is not running, so there is no automatic
  hosted all-push admission. Do not treat disabled or skipped checks as passed, and
  do not infer approval of hosted all-push admission.
- TAL-194 remains open and separate: the full cloud/provider lifecycle — project
  apply, workload, teardown and independent audit — is not demonstrated by this
  source.
- `qualify-ha.yaml` is manually triggered (`workflow_dispatch` with a reviewed
  40-hex `source_revision`), but publication and qualification are paused pending
  the isolated self-hosted replacement and its successful push/main `Validate`
  evidence. Its gate still requires a successful exact-SHA `Validate` run with
  `event=push` and `branch=main`, so it cannot pass while no such run exists. Do not
  weaken the gate or claim the replacement exists. When it runs, it publishes a
  multi-arch image to GHCR and runs local kind-based failover qualification; it does
  not touch GCP.
- No workflow here performs a cloud apply or teardown.

## Secrets

Workflows use only the ephemeral `github.token`; no repository secrets are in
use. Cloud identity is designed around GitHub OIDC federation
([bootstrap](docs/cloud-bootstrap.md)), and bootstrap still needs an authorized
operator's short-lived login. Never commit state, plans, service-account keys or
credentials.

## Preserving work

Per-run recovery and closure design is in open PR #5 and is **not** on `main`.
Do not reimplement or edit it here, and do not describe its capabilities as
current. Other worktrees may hold in-flight work; do not reset, stash or clean
them.
