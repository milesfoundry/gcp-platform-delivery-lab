# Worker poll readiness and hosted HA qualification — 2026-10-06

Source `3b1abf3b791b4ab95f852c860786ca91d8c0a381` passed hosted Validate
[37517856995](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37517856995)
and Publish and qualify HA
[37518413015](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37518413015).
The published image is
`ghcr.io/talisman36935/report-workshop@sha256:dfba95425f82b619987f307e63e2e2720f9395a5a2c7836ec2d2a25f7d398d23`.
The release artifact records source/image/platform digests and declares runtime
qualification pending at publication time; the later workflow result below is
the runtime evidence, not a rewrite of that artifact.

The qualification passed native ARM migration/API/worker smoke and hosted
Kubernetes database/workload tests. Primary-pod promotion took 59.859 seconds.
After twenty accepted jobs were observed outstanding, loss of the worker hosting
the database primary recovered in 92.901 seconds. Two API and two worker
replicas remained ready, all acknowledged reports matched the golden output,
the test-only completion gate was removed and the cluster was deleted. The
simulation used three labelled workers on one hosted machine; it did not provision
cloud infrastructure or verify physical-zone failure.

Anonymous verification fetched and SHA256-checked both platform manifests, their
configs and all runtime layers: 28,946,103 bytes across AMD64/ARM64, without an
account credential. The detailed digest list is in
[anonymous-pull.json](anonymous-pull.json).

## Preserved failed attempts

- Run [37511656967](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37511656967)
  published source `0ef279c` but failed native ARM smoke because the test worker
  did not start its private metrics/readiness listener. Kubernetes qualification
  also failed during node loss. The cluster deletion and ARM-process cleanup
  steps passed. This led to explicitly setting `METRICS_ADDR` in the ARM job.
- Run [37513731105](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37513731105)
  passed ARM but timed out waiting for two ready API and worker replicas after
  node loss. Its cluster was deleted.
- Run [37515849648](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37515849648)
  passed ARM but repeated the survivor-readiness timeout. Its allowlisted
  observation recorded CloudNativePG `Failing over`, two of three ready database
  instances and unready worker pods; the named cluster was deleted. The subsequent
  change records a successful queue/claim poll before job analysis/commit, matching
  the intended poll-health contract. Run 37518413015 then passed the full gate.

These are local hosted Kubernetes/PostgreSQL experiments, not provider-service
qualification or a production availability guarantee.
