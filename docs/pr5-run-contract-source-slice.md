# PR5 run-contract source slice

Status: partial, unmerged source extraction candidate. This is not a merge,
qualification, integration, or closure record.

## Provenance

- Base: `origin/main` at `baf38cc7ada62b57735472f47f0618d35287da21`.
- Donor: unmerged GCP PR5 draft head
  `d5e1e27857198a519a8e54acca710dc917b74d8c`.
- The donor is 62 commits ahead of the base and changes 71 paths; the donor is
  not an ancestor of `origin/main`.

The two files below were copied with `git show <donor>:<path>` and are
byte-identical to the donor blobs. Neither path exists on the base, so this
slice does not overwrite main content.

| Path | Git blob | Bytes |
| --- | --- | --- |
| [scripts/run_contract.py](../scripts/run_contract.py) | `e9acdcd591cb1d36f7b9a9c55ce346d4a62f143f` | 73,078 |
| [scripts/run_contract_cli.py](../scripts/run_contract_cli.py) | `24d8d963570d6cdc5643d3e7e81941997cac2505` | 2,129 |

## Responsibilities and import boundary

`scripts/run_contract.py` performs credential-free validation of run intents
and manifests, checks monotonic lifecycle transitions, and computes deterministic
dry-run plans for workload and recovery-control cleanup. It does not
authenticate, call provider APIs, persist a ledger, or delete resources.

`scripts/run_contract_cli.py` is a local JSON-file CLI for intent, manifest, and
dry-run cleanup modes. It imports `run_contract` as a sibling module, so the two
files must remain together in `scripts/`. Its output is a validation summary or
a dry-run plan; it does not execute provider or workload operations.

Both files use only Python standard-library imports plus the local sibling
import in the CLI. Verification used AST parsing only: no module import,
execution, dependency installation, test run, or provider access.

## Main and donor status

At the stated base, `origin/main` contains neither script. The donor remains
unmerged. This candidate adds only the two scripts and this document; all other
PR5 paths remain in the donor and are not represented here.

## Historical donor test references

The donor contains historical tests for this source:

- [donor `scripts/test_run_contract.py`](https://github.com/milesfoundry/gcp-platform-delivery-lab/blob/d5e1e27857198a519a8e54acca710dc917b74d8c/scripts/test_run_contract.py)
  (blob `699dbf13213381b2e10717673a80203623e8695c`)
- [donor `scripts/test_run_contract_cli.py`](https://github.com/milesfoundry/gcp-platform-delivery-lab/blob/d5e1e27857198a519a8e54acca710dc917b74d8c/scripts/test_run_contract_cli.py)
  (blob `652e85a59fea09c6e8e8d32ee180bf4b75df6f54`)

The two fixtures have now been copied byte-identically from that donor for
qualification of this isolated candidate. Their source was not rewritten. No
passing behavior is claimed until the named offline batch has been reviewed.

## Limits and remaining gaps

This is a source candidate for review in smaller pieces. It does not fulfill the
full run-contract/ledger slice or G11 closure. It excludes the ledger,
controller, provider adapters, reaper, Terraform, workflows, requirements,
identity and all other fixture/test changes from PR5. It does not publish lab guidance
under TAL221 and grants no GCP, hosted-runner, or image authority.

Remaining acceptance gaps include the full run-contract/ledger slice, reviewed
offline qualification, integration evidence, G11 closure and the remaining donor
paths. The original extraction changed two source paths; its two donor fixture
paths are now also present in this review candidate.
Qualification here is limited to byte equality, AST syntax and import inspection,
relative-link checking, and diff review.
