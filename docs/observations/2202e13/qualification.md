# Source qualification: `2202e139`

The exact source revision `2202e13962914f71e56b146ff7632282b9d04492` passed
[GCP Validate run 37687488181](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37687488181)
and [Publish and qualify HA run 37687979581](https://github.com/milesfoundry/gcp-platform-delivery-lab/actions/runs/37687979581).
The immutable multi-architecture image is
`ghcr.io/talisman36935/report-workshop@sha256:3c08b2754404fdc14dda81ee4e0bc9f9f640a3223ccd89988f19e74250c41816`.

The workflow's native ARM runtime job passed migration, API/worker startup and
golden-report smoke. Its Kubernetes job passed PostgreSQL primary promotion,
workload-node loss, database re-promotion, and workload recovery. At node loss,
20 accepted jobs were pending; two API and two worker replicas survived and
accepted reports were preserved. The disposable kind cluster was deleted. The
[hosted observation](kubernetes-ha.json) records exact timings and sanitized job
identifiers.

The separate [anonymous-pull observation](anonymous-pull.json) records a
credential-free download and SHA256 check of the image index, AMD64/ARM64 platform
manifests, configs and all runtime layers (28,921,673 bytes). The original
[publication record](image-release.json) remains unchanged: it records that
anonymous pull, runtime qualification and signature verification were pending at
publication time. The later qualification does not establish a verified signature.

Scope: the Kubernetes test used three labelled workers simulated on one hosted
machine. No cloud resources were provisioned; no physical/cloud zone, cloud
storage, IAM or cloud telemetry failure was tested. These results do not qualify
AWS/EKS or a production availability objective.
