# Provider-free source integration candidate

Status: unmerged PR6 source-integration candidate, head
`e63d26ad8afbc9b987d2d72c0e9d4418d94100a7`, based on main
`9a51ec80f76b1425d1316c13fc153f68bbcc0f50`. Codex retains merge and admission.
The candidate's source scope is exactly three provider-free modules at
blob-identical revisions from reviewed donor
`85fd4c6061de67217df60d7ca885b8951b62a034`. Fixtures are not copied into this
candidate, and no test suite, CI run or runtime check was executed for this
head. Earlier evidence below is historical.

## Source modules and provenance

| Module | Git blob | Bytes | Responsibility |
| --- | --- | --- | --- |
| [scripts/run_contract.py](../scripts/run_contract.py) | `e9acdcd591cb1d36f7b9a9c55ce346d4a62f143f` | 73,078 | Credential-free validation of run intents and manifests, monotonic lifecycle checks, deterministic dry-run plans |
| [scripts/run_contract_cli.py](../scripts/run_contract_cli.py) | `24d8d963570d6cdc5643d3e7e81941997cac2505` | 2,129 | Local JSON-file CLI for intent, manifest and dry-run cleanup modes |
| [scripts/terraform_backend_config.py](../scripts/terraform_backend_config.py) | `9acb7bcbe87ef4727a339658bd50f97a12af67ab` | 5,288 | Validates internal project metadata and emits GCS backend HCL containing only `bucket` and `prefix` |

All three module blobs are unchanged from `85fd4c6`. The CLI imports
`run_contract` as a sibling, so the CLI and its module stay together in
`scripts/`. Both run-contract modules use only the Python standard
library plus that sibling import. The backend helper is the reviewed `85fd4c6`
version; relative to original PR5 donor `d5e1e27`, it intentionally omits the
unsupported `project` line, consistent with the
[GCS backend reference](https://developer.hashicorp.com/terraform/language/backend/gcs)
(cited, not fetched). None of these modules authenticates, calls a provider,
persists a ledger or deletes resources.

## Donor fixtures

These immutable fixtures remain on reviewed donor `85fd4c6`; they are not part
of this integration candidate.

| Fixture | Git blob | Bytes |
| --- | --- | --- |
| [scripts/test_run_contract.py](https://github.com/milesfoundry/gcp-platform-delivery-lab/blob/85fd4c6061de67217df60d7ca885b8951b62a034/scripts/test_run_contract.py) | `699dbf13213381b2e10717673a80203623e8695c` | 41,865 |
| [scripts/test_run_contract_cli.py](https://github.com/milesfoundry/gcp-platform-delivery-lab/blob/85fd4c6061de67217df60d7ca885b8951b62a034/scripts/test_run_contract_cli.py) | `652e85a59fea09c6e8e8d32ee180bf4b75df6f54` | 5,929 |
| [scripts/test_terraform_backend_config.py](https://github.com/milesfoundry/gcp-platform-delivery-lab/blob/85fd4c6061de67217df60d7ca885b8951b62a034/scripts/test_terraform_backend_config.py) | `1d560c630ba5ecf03b04225987a4f6e20288a636` | 3,683 |
| [scripts/test_terraform_backend_cli.py](https://github.com/milesfoundry/gcp-platform-delivery-lab/blob/85fd4c6061de67217df60d7ca885b8951b62a034/scripts/test_terraform_backend_cli.py) | `9e148d04f69a6e0923c1a0aed654e3ba99a40f7c` | 4,008 |

## Existing evidence

No evidence was regenerated. The links and hashes below retain the accepted
historical records.

### Run-contract batch

Six committed shards,
[contract-shard-01.json](evidence/tal212/contract-shard-01.json) through
[contract-shard-06.json](evidence/tal212/contract-shard-06.json), are aggregated
in [contract-summary.json](evidence/tal212/contract-summary.json).

- Tested revision `a852f53e690e60e2341246643a4c5950bf165630`; evidence commit
  `17cb778ae965a0a6d4aad8b3d0bc8004dc0a6932` on
  `refactor/tal212-run-contract-slice`. The evidence commit only adds the shards.
- 43 unique donor methods across six disjoint shards (8 + 7 + 7 + 7 + 7 + 7),
  each shard exit 0 with expected count equal to actual and no failures, errors
  or skips.
- Scope is the local, provider-free behavior of `run_contract.py` and
  `run_contract_cli.py` as exercised once by those methods at `a852f53`. Runtime
  was Python 3.12.3 with no network, credentials, provider calls or installs.

### Backend helper observations

[backend-integration-review.json](evidence/tal212/backend-integration-review.json)
records five corrected-worker tests with zero failures, bound to
`terraform_backend_config.py` blob `9acb7bc...` and test blob `1d560c6...`, plus
an original four-case CLI baseline with one expected failure demonstrating the
old `project` defect. Four later historical CLI case observations,
[backend-cli-01.json](evidence/tal212/backend-cli-01.json) through
[backend-cli-04.json](evidence/tal212/backend-cli-04.json), each recorded one
pass on candidate `b834cba569737949a6348320090e16d25173d160`. That candidate is
not this head. None of this proves real Terraform GCS initialization, provider
or IAM behavior, bucket ownership or cloud activation.

## Excluded scope

Excluded from this candidate: provider adapters, ledger, controller, reaper,
identity/IAM, Terraform and cloud runtime, workflows, requirements, and all
other paths from PR5. It does not close full PR5, does not demonstrate TAL-194
and does not close G11.

## CI pause

The legacy `Validate` workflow is owner-disabled under the completed TAL-221
CI-control decision. Disabled or skipped checks are not passes; no CI ran for
this head, and `qualify-ha` publication and qualification remain paused. This
record grants no hosted-runner, GCP, provider, or image authority.
