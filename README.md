# GCP Platform Delivery Lab

[![Validate](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/workflows/validate.yaml/badge.svg)](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/workflows/validate.yaml)

A portfolio lab comparing Terraform-managed infrastructure and Config Sync
delivery with the [AWS reconciliation lab](https://github.com/milesfoundry/aws-kubernetes-reconciliation-lab).
Both use **Report Workshop**, a small deterministic asynchronous Go application.

**Status: first implementation slice, not a completed cloud demonstration.**
The local application is runnable. Terraform validates without credentials.
The Config Sync source contains a namespace/delegation demonstration, not an
application deployment. No GCP project has been provisioned by this repository.
CI runs on GitHub-hosted runners; the estate's shared self-hosted group is
private-only and not available to this public repository.

## Try the application locally

Requirements: Docker with Compose v2+, Python 3.10+, free localhost ports
18080, 19090 and 19091.
The first build downloads public images and Go dependencies.

```sh
git clone https://github.com/milesfoundry/gcp-platform-delivery-lab.git
cd gcp-platform-delivery-lab/workload
docker compose up --build -d
python3 smoke.py
docker compose logs --tail=50 api worker
docker compose down
```

The API is bound to **127.0.0.1:18080**. PostgreSQL is not published to the host.
The explicitly synthetic password in Compose is local-only, not a cloud credential.
Keep this unauthenticated demonstration off public ingress.

```sh
curl -sS http://127.0.0.1:18080/v1/jobs \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-001' \
  -d '{"fixture":"tiny-v1","algorithm":"tokens-v1"}'
# Use the returned id:
curl -sS http://127.0.0.1:18080/v1/jobs/JOB_ID
curl -sS http://127.0.0.1:18080/v1/jobs/JOB_ID/report
```

The golden report contains 3 documents, 11 tokens, 6 unique tokens and 1 duplicate
document. Reusing a key with identical input returns the same job; different input
returns 409. Named fixture allowlisting prevents arbitrary files/URLs being processed.

`docker compose down` retains the local database. To intentionally erase this
demo's synthetic job history, run `docker compose down --volumes` from `workload/`.
Do not use this command against another Compose project.

## Checks

Credential-free, from the repository root. Run these instead of any cloud
command:

```sh
cd workload
gofmt -l cmd internal            # must print nothing
go vet ./...
go test ./...
cd ..
terraform fmt -check -recursive infrastructure bootstrap
terraform -chdir=infrastructure/lab init -input=false -lockfile=readonly
terraform -chdir=infrastructure/lab validate
terraform -chdir=infrastructure/lab test
python3 scripts/check-ownership.py
```

`terraform validate` and `terraform test` prove the configuration parses and its
contract holds; they are not permission to apply, and this repository has never
been applied to a cloud project. The full gate list is in
[the runbook](docs/runbook.md), and every command the `Validate` workflow runs is
listed in [AGENTS.md](AGENTS.md).

## What is implemented?

| Area | Current implementation | Not yet demonstrated |
| --- | --- | --- |
| Workload | API, PostgreSQL outbox, fenced worker, Pub/Sub/GCS and SQS/S3 adapters, immutable report objects | Live cloud delivery, orphan sweep and object export |
| Tests | Golden output, idempotency, fenced recovery, SDK protocol doubles, real PostgreSQL, native ARM smoke, hosted Kubernetes promotion and primary-worker loss | Live cloud queue/IAM contracts, physical cloud-zone/CSI failure |
| GCP | Terraform lab foundation plus private-state/federated-CI bootstrap | Actual plan/apply, identity qualification and cloud teardown |
| GitOps | Root ownership, restricted delegation, migration gates; companion hosted Flux app promotion/rollback/drift/denial test | Config Sync/cloud runtime, binary/schema rollback, provider networking/identity and lifecycle |
| Observability | Structured logs, metrics/Prometheus, optional OTel collector/Tempo and CPU/heap capture | Continuous profiles, log backend and investigation dashboards |
| Portfolio | Strict local baseline records, missing-collector gate, historical investigation evidence | Qualified durable cloud bundles and website ingestion |

## Navigate

- [Ephemeral London HA architecture, instance comparison and cost gates](docs/architecture-cost.md)

- [Current architecture and decisions](docs/implementation.md)
- [Application replicas, crash recovery and dormant Kubernetes profile](docs/application-replicas.md)
- [Image publishing and hosted database/node-failure qualification](docs/image-and-ha-qualification.md)
- [Cloud queue/object adapters and crash contracts](docs/cloud-adapters.md)
- [Opt-in Config Sync/Flux workload delivery](docs/gitops-delivery.md)
- [Validation and GCP activation boundary](docs/runbook.md)
- [Private state and federated CI bootstrap](docs/cloud-bootstrap.md)
- [Local metrics, evidence recorder and outage check](docs/local-observability.md)
- [Trace correlation and CPU/heap investigation](docs/local-tracing-profiling.md)
- [Controlled regression and original-image recovery](docs/local-regression-recovery.md)
- [OpenAPI 3.1 contract](workload/internal/httpapi/openapi.json) (served at /openapi.json)
- [Repository guidance for agents](AGENTS.md): edit locations, the exact check
  commands and the paid-operation boundary

Implementation records describe current behavior; the architecture profile distinguishes
validated configuration from capabilities still requiring live qualification.
