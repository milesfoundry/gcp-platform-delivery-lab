# Ephemeral HA architecture and cost policy — 2026-10-06

This profile combines multi-zone availability with short-lived execution and
cost-conscious node selection. Capacity, failover and cleanup are qualification
requirements, not inferred from a successful deployment.

## Target topology and evidence

- London: GCP europe-west2 / AWS eu-west-2. One cloud experiment at a time.
- GCP: regional GKE Standard control plane, three workers across three zones,
  Terraform and fleet-managed Config Sync RootSync/delegated RepoSync.
  Explicitly select one node per zone, not the regional nine-node default.
- AWS: EKS multi-AZ control plane, three workers across three AZs, temporary kind
  management with Flux/CAPI/CAPA and ACK S3/SQS. Management remains until cleanup.
- Preserve clean application ports, replicated API/workers, durable PostgreSQL,
  Pub/Sub/GCS or SQS/S3, scoped identities and complete GitOps delivery/recovery.
- Include topology spread/anti-affinity, disruption budgets, probes, controlled
  failover and metrics/logs/traces/profiles/dashboards.
- Database HA requires a qualified replication/failover and storage design with
  measured recovery/data-loss criteria. Multiple nodes/API replicas do not prove
  end-to-end HA; the single-PostgreSQL Compose baseline is not HA.
- Keep networking/NAT, ingress/load balancing and policy components needed to
  substantiate the architecture; cost them rather than remove them by default.
  Avoid gratuitous duplicate clusters. Cloud Deploy and CAPI pivot remain later
  separately owned experiments, not prerequisites for every HA run.
- The portfolio shows both complete design and exact tested deployment, clearly
  separating intended capabilities from observed failover/recovery/cost/cleanup.

## London instance comparison

Snapshot checked 2026-10-06. Linux on-demand USD before tax/credits, excluding
CPU-credit surcharges; no reserved/committed-use purchases for temporary labs.

| Candidate | vCPU / RAM | Node/hour | Three nodes/hour |
| --- | --- | ---: | ---: |
| AWS t4g.small (ARM) | 2 / 2 GiB | $0.0188 | $0.0564 |
| AWS t4g.medium (Graviton ARM) | 2 / 4 GiB | $0.0376 | $0.1128 |
| AWS t3a.medium (x86 fallback) | 2 / 4 GiB | $0.0425 | $0.1275 |
| AWS t4g.large (ARM) | 2 / 8 GiB | $0.0752 | $0.2256 |
| AWS t3a.large (x86) | 2 / 8 GiB | $0.0850 | $0.2550 |
| GCP e2-medium (shared CPU: 1 fractional vCPU) | 2 exposed / 4 GiB | $0.03350571 | $0.10051713 |
| GCP e2-standard-2 | 2 / 8 GiB | $0.06701142 | $0.20103426 |

Start with the smallest plausible nodes: three AWS t4g.medium (Graviton) and
three GCP e2-medium (2 vCPUs exposed, 1 fractional vCPU). The 2 GiB t4g.small or
e2-small tiers are deliberately excluded: each
zone must fit a synchronous database replica, one API and one worker, plus the
node OS, Kubernetes and network agents. The declared app+database requests alone
are about 0.9 GiB and limits total 2 GiB per zone before system overhead. This
4 GiB choice is a qualification candidate, not a capacity guarantee. Measure
actual allocatable memory, pressure, CPU throttling and two-node survivor capacity.
Only move to t4g.large/e2-standard-2 if a recorded test demonstrates the 4 GiB
candidate cannot pass; do not silently resize or reduce HA replicas.

Including the $0.10/hour standard managed control-plane fee gives candidate
node/control-plane subtotals of $0.2128/hour AWS and $0.20051713/hour GCP,
approximately $0.11/$0.10 for 30 minutes at steady topology. The 8 GiB fallbacks
are approximately $0.3256/hour AWS and $0.30103426/hour GCP before other services.
These are NOT complete run estimates: add disks, HA database, NAT/IPs/LBs, transfer,
telemetry, Config Sync feature fees where applicable, queues/storage, registry,
temporary identity/alert bootstrap and provisioning/deletion/surge-node time.

Start on-demand for repeatable qualification. Compare live Spot offers for a
separate interruption profile, not a fixed assumed discount. Burstable AWS CPU
credit charges/throttling need explicit load-test treatment. Recheck pricing and
regional/version availability before apply.

Sources:
- [GCP VM prices: London selection](https://cloud.google.com/products/compute/pricing/general-purpose)
- [EKS pricing](https://aws.amazon.com/eks/pricing/)
- [GKE pricing and feature fees](https://cloud.google.com/kubernetes-engine/pricing)
- [GKE regional node defaults](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/regional-clusters)
- [AWS T4g CPU-credit pricing](https://aws.amazon.com/ec2/instance-types/t4/)
- [AWS NAT billing: partial hours round up](https://aws.amazon.com/vpc/pricing/)
- [GCP NAT pricing](https://cloud.google.com/nat/pricing)

## Alerting, cost safeguards and immediate teardown

Approved starting planning envelope: estimate £5 gross usage per cloud run and
£10 for the first GCP+AWS pair, with a 60-minute maximum from first billable
resource. These are monitoring and planning reference points, not hard caps or
automatic no-go thresholds. The primary cost objective is to monitor gross usage,
net cash charges, remaining promotional-credit balance/expiry and projected
runway, then destroy all run resources promptly whenever the lab is idle. Keep
gross usage visible separately from credits and net cash charges; do not assume
credits will cover every service or arrive before charges are due.

Configure layered notifications before activation:

- GCP project budget: actual gross-cost alerts at 25%, 50%, 75%, 90% and 100%,
  plus forecast alerts at 75% and 100% where available; route to the user-provided
  GCP lab mailbox through a verified Monitoring email channel.
- AWS account budget: actual gross-cost alerts at 25%, 50%, 75%, 90% and 100%,
  plus forecast alerts at 75% and 100% when forecast data exists; use a confirmed
  SNS email subscription to the user-provided AWS lab mailbox.
- The not-yet-built cloud lifecycle workflow must use the provider event publisher
  for notices (created, ready, 15 minutes to expiry, teardown started,
  teardown/audit passed or failed) to the same provider-specific mailbox. GCP's
  log-alert emitter is implemented but not yet scheduled or connected to a cloud
  run; delivery has not been tested.
- At 75% of the planning amount, review actual usage and projected credit runway;
  at 90%, stop optional work and begin teardown if continued execution risks
  exhausting available credits or creating an unapproved charge. At expiry or
  when idle, the independent janitor deletes the run even if the orchestrator is
  unavailable. Alert delivery failure is a preflight failure, not a warning to
  ignore.

Email values belong in private deployment configuration, not these public repos.
Verify end-to-end delivery before provisioning. Billing notifications are delayed
signals, not a real-time circuit breaker. GKE/Compute are not currently among
Google's eligible spend-cap services; AWS Budgets may also report after usage.
Therefore retain the independent wall-clock watchdog, run inventory and provider
cleanup/audit. Resource limits, alerts, deadlines and cleanup are operational
safeguards, not a guaranteed monetary hard cap. The lack of a provider-wide cap
is not by itself a blocker; before each run, record available credit and expiry,
review the full cost estimate against the remaining runway, confirm alerts and
teardown, and obtain explicit approval if the estimate could consume the runway
or incur unplanned cash charges.

Begin teardown no later than minute 40 and require independent inventory audit by
minute 60. Brief workload time does not mean instant provisioning/deletion; include
the entire setup and cleanup interval in the cost estimate.

Capture evidence, then immediately delete on success, failure, cancellation or
expiry. Keep external scoped inventory and independent cleanup capability.
Audit compute/clusters, disks/snapshots, NAT/LBs, interfaces/IPs, buckets/object
versions, queues and run-scoped logs/images. Insufficient audit permissions mean
incomplete cleanup evidence. Record eventual deletion and delayed billing separately.

Terraform state now uses local `.terraform/` files instead of a retained GCS state
bucket. Delete the operator/run workspace only after independent provider inventory
confirms teardown; never upload state to workflow artifacts/cache. Federation and
alert bootstrap resources still need explicit destruction after each cloud cycle.
The independent expiry janitor is not implemented, so cloud activation remains
blocked until it survives runner loss and has a verified least-privilege delete path.
Portfolio evidence is sanitized historical data, not live control/credentials.

## Implementation status

GCP Terraform defines a regional London cluster with one e2-medium worker per
zone. AWS renders three one-node zonal groups using AL2023 ARM for t4g.medium or
x86 for t3a.medium; the 8 GiB sizes are documented fallback only. GCP includes a
log-matched email alert for allowlisted lifecycle events; AWS includes an SNS
lifecycle publisher. Cost alerts and private recipient inputs are modeled, but no
cloud budget, channel, or lifecycle notification has been created or delivery-tested.
The providers' emitters are not wired to a run workflow/scheduler. Resource fit,
cloud placement/admission, workload/database HA, networking, failover, independent
expiry cleanup and spending protections still require live qualification. No cloud
deployment or billing configuration is claimed by static tests.

Separate [hosted Kubernetes qualification](image-and-ha-qualification.md) passed
three-instance PostgreSQL promotion, primary-worker loss, preservation of all 31
golden reports and named-cluster cleanup. Public AMD64/ARM64 image content and
native ARM execution are verified. Simulated zones/local-path disks on one runner
do not qualify London cloud zones, CSI, IAM, capacity or cloud spending protection.
