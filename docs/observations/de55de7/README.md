# Local replica and worker-crash observation

[Hosted run 37385769512](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37385769512)
passed at source de55de7c084313114bb2cb594b343f1bcd4a5f3d.
The retained JSON records shared API idempotency, a real SIGKILL/exit 137,
successful attempt-two recovery after natural lease expiry, golden report equality
and test-gate removal/baseline restoration. Only one attempt completed.

This is local Compose process continuity with a single PostgreSQL database.
It does not establish database, Kubernetes, AZ or cloud queue HA. The Kubernetes
profile was schema-checked only; no cloud resources were created.
