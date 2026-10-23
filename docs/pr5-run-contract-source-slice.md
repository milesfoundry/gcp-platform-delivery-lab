# PR5 run-contract source slice

Status: partial, unmerged source extraction candidate. Locally qualified on
2026-10-23 for provider-free two-module behavior only. This is not a merge,
integration, deployment, provider, or closure record.

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

The two copied donor fixtures are also byte-identical to the donor:

| Fixture | Git blob | Bytes |
| --- | --- | --- |
| [scripts/test_run_contract.py](../scripts/test_run_contract.py) | `699dbf13213381b2e10717673a80203623e8695c` | 41,865 |
| [scripts/test_run_contract_cli.py](../scripts/test_run_contract_cli.py) | `652e85a59fea09c6e8e8d32ee180bf4b75df6f54` | 5,929 |

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
import in the CLI. The initial extraction verification used AST parsing only:
no module import, execution, dependency installation, test run, or provider
access at that stage.

## Main and donor status

At the stated base, `origin/main` contains neither script. The donor remains
unmerged. The source extraction adds the two scripts; this review candidate also
contains the two donor fixtures and the local evidence described below. All
other PR5 paths remain in the donor and are not represented here.

## Historical donor test references

The donor contains historical tests for this source:

- [donor `scripts/test_run_contract.py`](https://github.com/milesfoundry/gcp-platform-delivery-lab/blob/d5e1e27857198a519a8e54acca710dc917b74d8c/scripts/test_run_contract.py)
  (blob `699dbf13213381b2e10717673a80203623e8695c`)
- [donor `scripts/test_run_contract_cli.py`](https://github.com/milesfoundry/gcp-platform-delivery-lab/blob/d5e1e27857198a519a8e54acca710dc917b74d8c/scripts/test_run_contract_cli.py)
  (blob `652e85a59fea09c6e8e8d32ee180bf4b75df6f54`)

The two fixtures have now been copied byte-identically from that donor for
qualification of this isolated candidate. Their source was not rewritten. At the
time of copying, no passing behavior was claimed pending review; the named
offline batch has since been reviewed and is recorded below.

## Local qualification

Six committed shards, [contract-shard-01.json](evidence/tal212/contract-shard-01.json)
through [contract-shard-06.json](evidence/tal212/contract-shard-06.json), are
aggregated in [contract-summary.json](evidence/tal212/contract-summary.json).
The summary cites every shard and its counts.

- Tested revision: `a852f53e690e60e2341246643a4c5950bf165630`.
- Evidence commit: `17cb778ae965a0a6d4aad8b3d0bc8004dc0a6932` on
  `refactor/tal212-run-contract-slice`. That commit is not the tested revision;
  it only adds the six shard JSON files.
- Selected methods: 43 unique methods across six disjoint shards, counted as
  8 + 7 + 7 + 7 + 7 + 7.
- Result per shard: exit code 0, actual count equals expected count, and no
  failures, errors, or skips (recorded as either `0` or an empty list). Totals
  are 43 expected, 43 actual, 0 failures, 0 errors, 0 skipped.
- Runtime: Python 3.12.3 from the fixed locked virtual environment, executed
  locally and provider-free with no network, credentials, provider calls, or
  dependency installation.
- Byte binding: the source and fixture blobs listed above are unchanged between
  `a852f53` and `17cb778`, and remain byte-identical to donor `d5e1e27`. The
  only paths changed between those two revisions are the six shard JSON files.

The qualified behavior is limited to `scripts/run_contract.py` and
`scripts/run_contract_cli.py` as exercised by the 43 named existing donor test
methods once. It is not evidence of integration, cloud activation, provider or
IAM correctness, ledger/controller mutation or a cloud deployment.

## Limits and remaining gaps

This is a source candidate for review in smaller pieces and remains unmerged. It
does not fulfill the full PR5 run-contract/ledger slice or G11 closure. It
excludes the ledger, controller, provider adapters, reaper, Terraform, workflows,
requirements, identity and all other fixture/test changes from PR5. It does not
publish lab guidance under TAL221 and grants no GCP, hosted-runner, or image
authority. Provider, ledger, and controller behavior remain unqualified.

Remaining acceptance gaps include the full run-contract/ledger slice,
integration evidence, G11 closure, TAL221 admission, provider/IAM validation,
and the remaining donor paths. The original extraction changed two source paths;
its two donor fixture paths are now also present in this review candidate.
Earlier extraction checks were limited to byte equality, AST syntax and import
inspection, relative-link checking, and diff review. The local qualification
above adds only the 43 named offline methods; it does not close any of the gaps
listed here.
