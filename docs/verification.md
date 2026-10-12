# Verification record

## Durable tracing and bounded profiles — 2026-10-04

Source: `63372e9b3c3a83c4719d639cd42a5dec454ce313`.
[Hosted run 37164242715](https://github.com/talisman36935org/gcp-platform-delivery-lab/actions/runs/37164242715)
passed the real collector/Tempo integration, durable trace-parent and retry tests,
existing database/race gates, and CPU/heap capture. The CPU tool output contained
document-analysis frames, so a downloaded binary profile alone was not the gate.
This verifies local trace correlation and process profiling; the release-regression
comparison and trace-to-profile bridge remain outstanding.

## Local metrics and collector-outage gate — 2026-10-03

Source: `ce56202cfc03ce50382e4ea8d51c6de5b5b61032`.
[Hosted run 37158571002](https://github.com/talisman36935org/gcp-platform-delivery-lab/actions/runs/37158571002)
passed the existing application/infrastructure gates plus the real Prometheus
profile, metrics cardinality tests, strict evidence tests and recorder.

- [Baseline observation](observations/ce56202/baseline.json): 10/10 jobs completed,
  all golden reports matched, both process revisions matched, both collector
  targets were up.
- [Collector-outage observation](observations/ce56202/collector-outage.json):
  one further job completed correctly and both process metrics remained
  available; collector access failed and the evidence result was `failed`.
  That failure was expected and explicitly asserted by CI.

These are retained copies of the actual downloaded CI observations, not invented
fixtures. They are small local smoke observations, not cloud evidence, signed
bundles, performance benchmarks or proof of teardown. Their source revision and
timestamps identify the historical run; subsequent revisions need their own checks.

## Initial foundation — 2026-10-03

Source: `23b9632`.
[Hosted run 37145048417](https://github.com/talisman36935org/gcp-platform-delivery-lab/actions/runs/37145048417)
passed:

- Go formatting, vet and race tests against an isolated PostgreSQL service.
- Concurrent idempotency, conflict rejection, lease exclusion, stale completion
  rejection, successful retry and five-attempt exhaustion.
- Container build and Compose API/worker smoke test.
- Terraform format/init/validate and disjoint Config Sync ownership.

Observed golden report:

```json
{
  "documents": 3,
  "tokens": 11,
  "unique_tokens": 6,
  "duplicate_documents": 1,
  "input_sha256": "2522de9d1c28cf3a163c3703dbabb2b63009e5daa6a8d98f2aac85f577307bd5"
}
```

This record preserves the assertion/result after CI log retention expires.
It is an authored test record, not a signed portfolio evidence bundle. It proves
local behavior, not cloud IAM, GKE deployment or performance. Subsequent commits
have their own Validate runs; do not transfer this result to untested revisions.
