# Provider-free source integration

Status: merged on main as `6b88e2b90fc5b6d1e674712bfd6831eaf74442d0`
("TAL-209: integrate reviewed provider-free contract source (#6)"), whose parent
is `9a51ec80f76b1425d1316c13fc153f68bbcc0f50`. The source scope is exactly three
provider-free modules at blob-identical revisions from reviewed donor
`85fd4c6061de67217df60d7ca885b8951b62a034`. The donor's four test fixtures are
restored by the companion integration patch, so the tree that was actually
tested is that patch applied to `6b88e2b` — the **patched tree**, not `6b88e2b`
alone — and the offline `scripts` suite passed in it locally on 17 November
2026. Commit `1d98fbb` subsequently changed documentation wording only; the
tested module and fixture blobs are unchanged. No hosted CI run, live provider
call or cloud runtime check was executed
for that tree. `origin/main` was re-read read-only on 17 November 2026 and still
pointed at `6b88e2b`, so the target was current at that time; that check is not
standing currency. Earlier evidence below is historical.

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

The four fixtures are restored verbatim from donor `85fd4c6`; each restored blob
is byte-identical to the donor blob. Only these fixtures were taken from the
donor tree; its older guidance and documentation were not carried over.

| Fixture | Git blob | Bytes | Methods |
| --- | --- | --- | --- |
| [scripts/test_run_contract.py](../scripts/test_run_contract.py) | `699dbf13213381b2e10717673a80203623e8695c` | 41,865 | 41 |
| [scripts/test_run_contract_cli.py](../scripts/test_run_contract_cli.py) | `652e85a59fea09c6e8e8d32ee180bf4b75df6f54` | 5,929 | 2 |
| [scripts/test_terraform_backend_config.py](../scripts/test_terraform_backend_config.py) | `1d560c630ba5ecf03b04225987a4f6e20288a636` | 3,683 | 5 |
| [scripts/test_terraform_backend_cli.py](../scripts/test_terraform_backend_cli.py) | `9e148d04f69a6e0923c1a0aed654e3ba99a40f7c` | 4,008 | 4 |

Local offline re-execution, 17 November 2026, in a disposable clone of
`6b88e2b` with the fixture restore and documentation patches applied (Python
3.12.3, unshared
network namespace exposing only loopback, only `PATH`/`HOME`/`PYTHONPATH`
supplied, standard library only):

- `python3 -m unittest discover -s scripts -p 'test_*.py' -v` ran 58 tests and
  exited 0 in the patched tree: the 6 pre-existing
  `test_notify_lifecycle`/`test_state_contract` methods plus the 52 restored
  methods (41 + 2 + 5 + 4).
- The restored modules also pass individually: 41 + 2 + 4 + 5 methods.
- Four scripted temporary-copy mutations exercised selected run-ID, backend,
  cleanup-order and CLI rejection checks. These are illustrative rather than
  exhaustive. The scratch collector and sabotage harness are not included in
  this repository and their recorded results are not hosted CI artifacts.
- Independent review found that removal of the source revision, source image
  digest and finance credit-evidence digest guards was not caught by these
  fixtures. Those negative cases remain follow-up coverage work.

`Validate` would select these files if it ran: its `terraform` job already runs
`python3 -m unittest discover -s scripts -p 'test_*.py' -v`. But the workflow is
owner-disabled — GitHub reports `disabled_manually` under the completed TAL-221
CI-control decision, recorded in the repository README — so restoring the files
produces no hosted run, and a disabled or skipped check is not a pass. This
patch changes no workflow.

## Existing evidence

The links and hashes below retain the accepted historical records; they were not
regenerated and no historical JSON was edited. The 17 November local run above
re-executed the same restored methods in the patched tree and is a separate
observation.

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
pass on candidate `b834cba569737949a6348320090e16d25173d160`, distinct from the
merged source revision. None of this proves real Terraform GCS initialization,
provider or IAM behavior, bucket ownership or cloud activation.

## Excluded scope

Excluded from this source slice: provider adapters, ledger, controller, reaper,
identity/IAM, Terraform and cloud runtime, workflows, requirements, and all
other paths from PR5. It does not close full PR5, does not demonstrate TAL-194
and does not close G11.

## CI pause

The legacy `Validate` workflow is owner-disabled under the completed TAL-221
CI-control decision (`disabled_manually`; the `push`/`pull_request` declarations
and hosted labels remain in YAML, but the workflow does not run). Disabled or
skipped checks are not passes; no hosted CI was run for the restored fixtures,
and `qualify-ha` publication and qualification remain paused. This record grants no
hosted-runner, GCP, provider, or image authority.
