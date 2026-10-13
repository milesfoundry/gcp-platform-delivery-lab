# Adapter implementation qualification — 2026-10-06

Source: `0e0a6133a18e4cfb10aae4defabd5b8f52ec69b5`.

[Validate 37398071004](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37398071004)
passed the complete hosted pipeline. Fresh `go test -race -count=1 ./...` includes
real disposable PostgreSQL publication/processing fencing tests and actual SDKs
against local Pub/Sub/GCS and SQS/S3 protocol doubles. Application golden output,
HTTP smoke, telemetry/profiling, compiled regression/image recovery and independent
API-replica/worker-SIGKILL tests also passed. Builds collect linked dependency
licenses/notices and the Go license into the runtime image.

## Failed checks and remediation

| Run / source | Failed step | Cause | Fix / verification |
| --- | --- | --- | --- |
| [37396124138](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37396124138), `4156c578afa57b3cefcde6c89cac401dde877664` | Format, vet and durable race tests | Pub/Sub v2 generated clients use TopicAdminClient/SubscriptionAdminClient, not v1 PublisherClient/SubscriberClient names | `a2354a56bb3300587787668f0883fa073e416456`; full checks passed in 37397017549 and 37398071004 |
| [37396607720](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37396607720), `63730b8e5f62ccfca33987555ce852d5f5b63794` | Format, vet and durable race tests | GCS wire fixture omitted CRC32C response metadata, causing the SDK integrity check to reject the upload | `8c607047d4e7aea50ec4a3636b3855b3cacd8e08` returns actual Castagnoli checksum and size; integrity checks remain enabled; full verification above |

Additional changes isolate publication from consumption so a publishing outage
cannot block already delivered messages; enforce read-only schema readiness;
guard Flux's ready-instance CEL field correctly; and prepare disjoint ownership
profiles with narrow application RBAC. Contract/render tests are not evidence of
live GitOps workload reconciliation.

No cloud credentials, resources or spending were involved. These tests do not
qualify live IAM, broker DLQ/redrive, regional object durability or cloud networking.
Object download/export, orphan sweeping, independent teardown/TTL, approved cost
limits and actual GitOps application rollback/negative-RBAC tests remain pending.

Release-time image metadata and subsequent runtime evidence are separate records;
do not reinterpret a release record's initial `pending` flags as final results.

[Release 37398470462](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37398470462)
passed all publication/native ARM/hosted Kubernetes HA jobs. Adjacent
`image-release.json`, `anonymous-pull.json` and `kubernetes-ha.json` preserve the
exact source/image correspondence and independently observed results. Both delivery
renderers also ran against a clean checkout of this source and its release record;
the AWS wrapper subsequently matched its advanced lock and rendered successfully.
Rendering performed no cluster operation and the candidates remain blocked.
