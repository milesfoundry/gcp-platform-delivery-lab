"""GET-only GCP project, budget and recovery-control closure collector.

Authentication is supplied explicitly and transport is injected. The adapter
does not discover credentials, mutate cloud resources, or return raw provider
responses. Billing-link snapshots must be captured and retained before the
corresponding unlink/project deletion operations.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Callable, Protocol
from urllib.parse import quote, urlsplit

from closure_auditor import (
    CLOSURE_SCHEMA,
    assess_closure,
)
from independent_auditor import AuditReportError, manifest_fingerprint
from gcp_token_identity import (
    IdentityVerificationError,
    PRINCIPAL_EMAIL,
    verify_access_token_principal,
)
from run_contract import ContractError, validate_manifest


class Response(Protocol):
    status_code: int

    def json(self) -> object: ...


class ReadOnlyTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        params: dict,
        headers: dict,
        timeout: int,
        allow_redirects: bool,
    ) -> Response: ...


class ClosureCollectionError(RuntimeError):
    """A provider response cannot establish complete closure inventory."""


API_HOSTS = frozenset(
    {
        "cloudresourcemanager.googleapis.com",
        "serviceusage.googleapis.com",
        "cloudbilling.googleapis.com",
        "billingbudgets.googleapis.com",
        "run.googleapis.com",
        "cloudscheduler.googleapis.com",
        "iam.googleapis.com",
        "openidconnect.googleapis.com",
    }
)
MAX_PAGES = 10_000
PAGE_SIZE = 100
CONTROL_ENDPOINTS = {
    "run.jobs": (
        "run.googleapis.com",
        "v2/projects/{project}/locations/europe-west2/jobs",
        "jobs",
        "run",
    ),
    "scheduler.jobs": (
        "cloudscheduler.googleapis.com",
        "v1/projects/{project}/locations/europe-west2/jobs",
        "jobs",
        "scheduler",
    ),
    "iam.service_accounts": (
        "iam.googleapis.com",
        "v1/projects/{project}/serviceAccounts",
        "accounts",
        "iam",
    ),
}


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ClosureCollectionError("provider response is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _time_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ClosureCollectionError(
            "observation clock must return a timezone-aware time"
        )
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _url(host: str, path: str) -> str:
    result = f"https://{host}/{path.lstrip('/')}"
    parsed = urlsplit(result)
    if parsed.scheme != "https" or parsed.hostname not in API_HOSTS:
        raise ClosureCollectionError("closure URL is outside the Google API allowlist")
    return result


def _get_json(
    transport: ReadOnlyTransport,
    url: str,
    *,
    token: str,
    params: dict,
    allowed_statuses: tuple[int, ...] = (200,),
) -> tuple[int, dict]:
    if not isinstance(token, str) or not token.strip():
        raise ClosureCollectionError("an explicit non-empty bearer token is required")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        response = transport.get(
            url,
            params=params,
            headers=headers,
            timeout=30,
            allow_redirects=False,
        )
    except Exception:
        raise ClosureCollectionError("closure inventory request failed") from None
    if response.status_code not in allowed_statuses:
        raise ClosureCollectionError(
            f"closure inventory request returned HTTP {response.status_code}"
        )
    try:
        body = response.json()
    except Exception:
        raise ClosureCollectionError("provider response was not JSON") from None
    if not isinstance(body, dict):
        raise ClosureCollectionError("provider response must be a JSON object")
    return response.status_code, body


def _list_pages(
    transport: ReadOnlyTransport,
    url: str,
    *,
    token: str,
    collection_key: str,
    params: dict | None = None,
) -> tuple[list[dict], int, str]:
    query = dict(params or {})
    query["pageSize"] = PAGE_SIZE
    next_token = None
    seen_tokens = set()
    pages = []
    values = []
    while True:
        if next_token is not None:
            if next_token in seen_tokens:
                raise ClosureCollectionError("provider pagination token repeated")
            seen_tokens.add(next_token)
            query["pageToken"] = next_token
        status, body = _get_json(transport, url, token=token, params=query)
        del status
        if (body.get("unreachable") or body.get("unreachables")
                or body.get("warnings")):
            raise ClosureCollectionError(
                "provider reported incomplete closure inventory"
            )
        entries = body.get(collection_key, [])
        if (not isinstance(entries, list)
                or any(not isinstance(item, dict) for item in entries)):
            raise ClosureCollectionError("provider collection field is malformed")
        pages.append(body)
        values.extend(entries)
        token_value = body.get("nextPageToken")
        if token_value in (None, ""):
            break
        if not isinstance(token_value, str) or len(token_value) > 8192:
            raise ClosureCollectionError("provider pagination token is malformed")
        next_token = token_value
        if len(pages) >= MAX_PAGES:
            raise ClosureCollectionError("provider pagination exceeded the page limit")
    return values, len(pages), _digest(pages)


def capture_project_billing(
    transport: ReadOnlyTransport,
    project_id: str,
    bearer_token: str,
    *,
    observed_at: datetime,
) -> dict:
    """Capture a minimized, fingerprinted GetProjectBillingInfo observation."""
    if not isinstance(project_id, str) or not project_id:
        raise ClosureCollectionError("project_id is required")
    encoded_project = quote(project_id, safe="")
    url = _url(
        "cloudbilling.googleapis.com",
        f"v1/projects/{encoded_project}/billingInfo",
    )
    _status, body = _get_json(
        transport, url, token=bearer_token, params={}, allowed_statuses=(200,)
    )
    if (body.get("projectId") != project_id
            or not isinstance(body.get("billingEnabled"), bool)):
        raise ClosureCollectionError(
            "project billing response does not match the requested project"
        )
    account = body.get("billingAccountName") or None
    if account is not None and not isinstance(account, str):
        raise ClosureCollectionError("project billing account name is malformed")
    return {
        "project_id": project_id,
        "billing_enabled": body["billingEnabled"],
        "billing_account_id": account,
        "observed_at": _time_text(observed_at),
        "response_sha256": _digest(body),
    }


def capture_run_project_snapshot(
    transport: ReadOnlyTransport,
    project_id: str,
    bearer_token: str,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict:
    """Capture enabled APIs and all current service-account names pre-deletion."""
    if not isinstance(project_id, str) or not project_id:
        raise ClosureCollectionError("project_id is required")
    if not isinstance(bearer_token, str) or not bearer_token.strip():
        raise ClosureCollectionError("an explicit non-empty bearer token is required")
    project_url = _url(
        "cloudresourcemanager.googleapis.com",
        f"v3/projects/{quote(project_id, safe='')}",
    )
    status, project_body = _get_json(
        transport, project_url, token=bearer_token, params={}
    )
    if status != 200 or project_body.get("projectId") != project_id:
        raise ClosureCollectionError(
            "pre-deletion project lookup did not match the run"
        )
    project_number_raw = project_body.get("projectNumber")
    project_number = str(project_number_raw)
    if not project_number.isdigit() or project_number.startswith("0"):
        raise ClosureCollectionError(
            "project lookup returned an invalid project number"
        )

    service_url = _url(
        "serviceusage.googleapis.com",
        f"v1/projects/{project_number}/services",
    )
    services, service_pages, service_sha256 = _list_pages(
        transport,
        service_url,
        token=bearer_token,
        collection_key="services",
        params={"filter": "state:ENABLED"},
    )
    enabled_services = []
    for service in services:
        name = service.get("name")
        prefix = f"projects/{project_number}/services/"
        if (not isinstance(name, str) or not name.startswith(prefix)
                or not name[len(prefix):]
                or "/" in name[len(prefix):]
                or service.get("state") != "ENABLED"):
            raise ClosureCollectionError("enabled service inventory is malformed")
        enabled_services.append(name[len(prefix):])
    if len(set(enabled_services)) != len(enabled_services):
        raise ClosureCollectionError("enabled service inventory contains duplicates")
    enabled_services.sort()

    service_account_url = _url(
        "iam.googleapis.com",
        f"v1/projects/{quote(project_id, safe='')}/serviceAccounts",
    )
    accounts, account_pages, account_sha256 = _list_pages(
        transport,
        service_account_url,
        token=bearer_token,
        collection_key="accounts",
    )
    expected_prefix = f"projects/{project_id}/serviceAccounts/"
    service_accounts = []
    for account in accounts:
        name = account.get("name")
        if (not isinstance(name, str) or not name.startswith(expected_prefix)
                or not name[len(expected_prefix):]
                or "/" in name[len(expected_prefix):]):
            raise ClosureCollectionError("service-account inventory is malformed")
        service_accounts.append(name)
    if len(set(service_accounts)) != len(service_accounts):
        raise ClosureCollectionError("service-account inventory contains duplicates")
    service_accounts.sort()
    return {
        "project_id": project_id,
        "project_number": project_number,
        "observed_at": _time_text(clock()),
        "enabled_services": enabled_services,
        "service_accounts": service_accounts,
        "coverage": {
            "serviceusage.enabled_services": {
                "complete": True,
                "pages": service_pages,
                "resource_count": len(enabled_services),
                "response_sha256": service_sha256,
            },
            "run_project.service_accounts": {
                "complete": True,
                "pages": account_pages,
                "resource_count": len(service_accounts),
                "response_sha256": account_sha256,
            },
        },
    }


def collect_closure_report(
    manifest: object,
    *,
    billing_before: object,
    billing_after: object,
    project_snapshot: object,
    transport: ReadOnlyTransport,
    bearer_token: str,
    expected_auditor_email: str,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict:
    """Collect full project/budget/control closure coverage without mutations."""
    try:
        run = validate_manifest(manifest)
    except ContractError as exc:
        raise ClosureCollectionError(
            f"run manifest violates the contract: {exc}"
        ) from exc
    project_id = run["scope"]["project_id"]
    recovery_project_id = run["scope"]["recovery_project_id"]
    billing_account_id = run["finance"]["billing_account_id"]
    if not isinstance(billing_before, dict) or not isinstance(billing_after, dict):
        raise ClosureCollectionError(
            "pre- and post-unlink billing observations are required"
        )
    if not isinstance(project_snapshot, dict) or set(project_snapshot) != {
        "project_id", "project_number", "observed_at", "enabled_services",
        "service_accounts", "coverage",
    }:
        raise ClosureCollectionError(
            "complete pre-deletion project inventory is required"
        )
    if project_snapshot["project_id"] != project_id:
        raise ClosureCollectionError(
            "pre-deletion inventory belongs to a different project"
        )
    if (not isinstance(expected_auditor_email, str)
            or PRINCIPAL_EMAIL.fullmatch(expected_auditor_email) is None):
        raise ClosureCollectionError("expected auditor email is malformed")
    if not isinstance(bearer_token, str) or not bearer_token.strip():
        raise ClosureCollectionError("an explicit non-empty bearer token is required")
    cleanup_principal = run["cleanup"]["service_account"]
    scheduler_principal = (
        f"{run['run_id']}-sched@{recovery_project_id}.iam.gserviceaccount.com"
    )
    if expected_auditor_email in {cleanup_principal, scheduler_principal}:
        raise ClosureCollectionError(
            "closure auditor must be distinct from reaper and scheduler identities"
        )
    try:
        verified_user = verify_access_token_principal(
            transport, bearer_token, expected_email=expected_auditor_email
        )
    except IdentityVerificationError as exc:
        raise ClosureCollectionError(str(exc)) from None

    project_url = _url(
        "cloudresourcemanager.googleapis.com",
        f"v3/projects/{quote(project_id, safe='')}",
    )
    project_status, project_body = _get_json(
        transport, project_url, token=bearer_token, params={},
        allowed_statuses=(200, 404),
    )
    if project_status == 200 and project_body.get("projectId") != project_id:
        raise ClosureCollectionError("project lookup returned a different project")
    project_observed_at = _time_text(clock())
    project_exists = project_status == 200
    project_sha256 = _digest({"status": project_status, "body": project_body})

    budget_url = _url(
        "billingbudgets.googleapis.com",
        f"v1/{billing_account_id}/budgets",
    )
    budgets, budget_pages, budget_sha256 = _list_pages(
        transport,
        budget_url,
        token=bearer_token,
        collection_key="budgets",
        params={"scope": f"projects/{project_id}"},
    )
    budget_ids = []
    for budget in budgets:
        name = budget.get("name")
        if not isinstance(name, str):
            raise ClosureCollectionError("budget entry lacks a canonical resource name")
        budget_ids.append(name)
    budget_observed_at = _time_text(clock())

    normalized_controls = []
    coverage = {
        "resourcemanager.project": {
            "complete": True,
            "pages": 1,
            "resource_count": int(project_exists),
            "response_sha256": project_sha256,
        },
        "billing.project_info": {
            "complete": True,
            "pages": 2,
            "resource_count": 2,
            "response_sha256": _digest([billing_before, billing_after]),
        },
        "billing.budgets": {
            "complete": True,
            "pages": budget_pages,
            "resource_count": len(budget_ids),
            "response_sha256": budget_sha256,
        },
    }
    snapshot_coverage = project_snapshot["coverage"]
    for surface in ("serviceusage.enabled_services", "run_project.service_accounts"):
        if surface not in snapshot_coverage:
            raise ClosureCollectionError(
                f"pre-deletion inventory lacks {surface} coverage"
            )
        coverage[surface] = snapshot_coverage[surface]
    for surface, (host, path_template, collection_key, kind) in (
        CONTROL_ENDPOINTS.items()
    ):
        path = path_template.format(project=quote(recovery_project_id, safe=""))
        url = _url(host, path)
        params = {"showDeleted": "true"} if kind == "run" else {}
        entries, pages, fingerprint = _list_pages(
            transport,
            url,
            token=bearer_token,
            collection_key=collection_key,
            params=params,
        )
        if kind == "iam":
            get_name = lambda entry: entry.get("name")
            expected_prefix = f"projects/{recovery_project_id}/serviceAccounts/"
        else:
            get_name = lambda entry: entry.get("name")
            expected_prefix = (
                f"projects/{recovery_project_id}/locations/europe-west2/jobs/"
            )
        for entry in entries:
            name = get_name(entry)
            if (not isinstance(name, str) or not name.startswith(expected_prefix)
                    or not name[len(expected_prefix):]
                    or "/" in name[len(expected_prefix):]):
                raise ClosureCollectionError(
                    f"{surface} returned a malformed or out-of-scope resource"
                )
            resource_type = (
                "gcp.run.job" if kind == "run"
                else "gcp.scheduler.job" if kind == "scheduler"
                else "gcp.iam.service_account"
            )
            normalized_controls.append({"type": resource_type, "id": name})
        coverage[surface] = {
            "complete": True,
            "pages": pages,
            "resource_count": len(entries),
            "response_sha256": fingerprint,
        }
    controls_observed_at = _time_text(clock())
    observed_at = _time_text(clock())
    report = {
        "schema": CLOSURE_SCHEMA,
        "run_id": run["run_id"],
        "project_id": project_id,
        "recovery_project_id": recovery_project_id,
        "manifest_sha256": manifest_fingerprint(run),
        "observed_at": observed_at,
        "auditor_id": verified_user.auditor_id,
        "auditor_email": verified_user.email,
        "coverage": coverage,
        "project": {
            "exists": project_exists,
            "observed_at": project_observed_at,
            "response_sha256": project_sha256,
        },
        "project_snapshot": {
            "project_id": project_snapshot["project_id"],
            "project_number": project_snapshot["project_number"],
            "observed_at": project_snapshot["observed_at"],
            "enabled_services": project_snapshot["enabled_services"],
            "service_accounts": project_snapshot["service_accounts"],
        },
        "billing": {
            "before": billing_before,
            "after": billing_after,
        },
        "budget_scope": f"projects/{project_id}",
        "budget_observed_at": budget_observed_at,
        "budgets": sorted(budget_ids),
        "controls_observed_at": controls_observed_at,
        "controls": normalized_controls,
    }
    try:
        assess_closure(run, report)
    except AuditReportError as exc:
        raise ClosureCollectionError(
            f"collected closure report failed its contract: {exc}"
        ) from exc
    return report
