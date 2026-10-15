# Hosted local investigation — 2026-10-04

[Successful run 37164832226](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37164832226)
tested source `487679ffcd7125f1f1fd7878d5d84401bc7d6819` in local Compose on
a hosted runner. These allowlisted JSON observations are preserved in Git.

Twelve sequential batch jobs per phase measured median completion of 0.1477305s
baseline, 0.607707s compiled regression, and 0.141206s after recovery. Every
report matched the golden fixture; recovery restored the exact original image.
This is a bounded synthetic investigation, not a cloud benchmark or GitOps rollback.

The trace record verifies five spans across API and worker with a durable parent.
The correlation record verifies matching trace IDs in both process logs. CPU/heap
profile lengths and SHA-256 hashes are recorded; profile binaries remain in the
30-day CI artifact and are not permanently published here. The JSON records alone
cannot independently revalidate those binaries after artifact expiry.

The collector-outage record is deliberately failed evidence: a job still completed
with its golden report while the Prometheus target check was unavailable.
No cloud resources were provisioned. Cloud teardown, signed durable bundles and
portfolio ingestion are not established by this run.
