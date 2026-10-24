"""Read-only GCP finance gate for exact run-project activation.

Credit and alert-delivery observations are operator evidence retained by hash
in the run manifest. The live verifier independently reads the exact project,
billing association and alerts-only budget before any run-project API is enabled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from urllib.parse import quote, urlencode, urlsplit

from gcp_token_identity import verify_access_token_principal


ALLOWED_HOSTS = frozenset({
    "cloudresourcemanager.googleapis.com",
    "cloudbilling.googleapis.com",
    "billingbudgets.googleapis.com",
    "monitoring.googleapis.com",
})
PAGE_SIZE = 100
MAX_PAGES = 1000
MAX_CREDIT_AGE = timedelta(hours=6)
MAX_ALERT_EVIDENCE_AGE = timedelta(hours=24)
EXPECTED_THRESHOLDS = frozenset(
    {(0.25, "CURRENT_SPEND"), (0.5, "CURRENT_SPEND"),
     (0.75, "CURRENT_SPEND"), (0.9, "CURRENT_SPEND"),
     (1.0, "CURRENT_SPEND"), (0.75, "FORECASTED_SPEND"),
     (1.0, "FORECASTED_SPEND")}
)


class FinancePreflightError(RuntimeError):
    """Current finance evidence or a live GCP billing control is unsafe."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise FinancePreflightError("finance evidence is not canonical JSON") from None


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise FinancePreflightError(f"{field} is missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FinancePreflightError(f"{field} is malformed") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise FinancePreflightError(f"{field} must include a timezone")
    return result.astimezone(timezone.utc)


class GCPFinancePreflight:
    """Verify fresh operator finance evidence and the exact live alerts budget.

    The caller supplies short-lived user credentials, transport, and expected
    Google email. This class performs only GET requests and never discovers
    credentials or creates/modifies billing resources.
    """

    def __init__(
        self,
        transport,
        token_source,
        identity_transport,
        *,
        expected_email: str,
        expected_subject: str,
        timeout: int = 30,
    ):
        if transport is None or token_source is None or identity_transport is None:
            raise ValueError("explicit finance transport and user credentials are required")
        if not isinstance(expected_email, str) or "@" not in expected_email:
            raise ValueError("a verified finance operator email is required")
        if not isinstance(expected_subject, str) or not expected_subject.startswith("google-user-"):
            raise ValueError("a stable verified finance operator subject is required")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
            raise ValueError("timeout must be between 1 and 60 seconds")
        self._transport = transport
        self._token_source = token_source
        self._identity_transport = identity_transport
        self._expected_email = expected_email
        self._expected_subject = expected_subject
        self._timeout = timeout

    def verify(self, manifest: dict, *, observed_at: datetime) -> None:
        """Require current credit and alert-delivery evidence for this run."""
        at = self._aware(observed_at)
        finance = manifest["finance"]
        credit_observed = _time(finance.get("credit_observed_at"), "credit observation")
        alert_observed = _time(
            finance.get("alert_delivery_verified_at"), "alert delivery observation"
        )
        if credit_observed > at + timedelta(minutes=2):
            raise FinancePreflightError("credit observation is from the future")
        if at - credit_observed > MAX_CREDIT_AGE:
            raise FinancePreflightError("credit observation is stale")
        if alert_observed > at + timedelta(minutes=2):
            raise FinancePreflightError("alert delivery evidence is from the future")
        if at - alert_observed > MAX_ALERT_EVIDENCE_AGE:
            raise FinancePreflightError("alert delivery evidence is stale")
        for field in ("credit_evidence_sha256", "alert_delivery_evidence_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", str(finance.get(field, ""))) is None:
                raise FinancePreflightError(f"{field} is missing or malformed")
        if finance.get("alerts_verified") is not True:
            raise FinancePreflightError("budget alert delivery has not been attested")
        try:
            available_credit = Decimal(str(finance["available_credit_gbp"]))
            gross_estimate = Decimal(str(finance["gross_estimate_gbp"]))
            net_cash = Decimal(str(finance["net_cash_exposure_gbp"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            raise FinancePreflightError("registered finance estimate is malformed") from None
        if available_credit < gross_estimate:
            raise FinancePreflightError("verified credit does not cover the gross run estimate")
        if net_cash != 0:
            raise FinancePreflightError(
                "projected net cash exposure requires separate explicit spend authorization"
            )
        credit_expiry = _time(finance.get("credit_expires_at"), "credit expiry")
        if credit_expiry <= _time(manifest["deadline_at"], "run deadline"):
            raise FinancePreflightError("available credit expires before the run deadline")

    def verify_project_budget(self, manifest: dict, *, observed_at: datetime) -> dict | None:
        """Verify one monthly, project-filtered alerts-only budget after billing.

        Returns a minimized receipt for the durable ledger, or ``None`` when
        the cost operator has not yet attached the run-specific budget.
        """
        at = self._aware(observed_at)
        self.verify(manifest, observed_at=at)
        principal = self._principal()
        scope = manifest["scope"]
        finance = manifest["finance"]
        project_id = scope["project_id"]
        recovery_project_id = scope["recovery_project_id"]
        billing_account = finance["billing_account_id"]

        project_status, project = self._get(
            "cloudresourcemanager.googleapis.com",
            f"/v3/projects/{quote(project_id, safe='')}",
            recovery_project_id,
        )
        if project_status != 200 or not isinstance(project, dict):
            raise FinancePreflightError("exact run project cannot be verified")
        labels = project.get("labels")
        if (
            project.get("projectId") != project_id
            or project.get("displayName") != manifest["run_id"]
            or project.get("parent") != scope["parent"]
            or project.get("state") != "ACTIVE"
            or not isinstance(labels, dict)
            or labels.get("lab_run_id") != manifest["run_id"]
        ):
            raise FinancePreflightError("run project identity does not match the registered intent")
        project_number = project.get("projectNumber")
        if (
            not isinstance(project_number, str)
            or re.fullmatch(r"[0-9]{1,20}", project_number) is None
        ):
            raise FinancePreflightError("Google did not return a valid project number")

        billing_status, billing = self._get(
            "cloudbilling.googleapis.com",
            f"/v1/projects/{quote(project_id, safe='')}/billingInfo",
            recovery_project_id,
        )
        if (
            billing_status != 200 or not isinstance(billing, dict)
            or billing.get("projectId") != project_id
            or billing.get("billingEnabled") is not True
            or billing.get("billingAccountName") != billing_account
        ):
            raise FinancePreflightError(
                "run project is not linked to its exact reviewed billing account"
            )

        budgets = self._list_budgets(billing_account, recovery_project_id)
        scoped = [
            budget for budget in budgets
            if f"projects/{project_number}" in (
                budget.get("budgetFilter", {}).get("projects", [])
                if isinstance(budget.get("budgetFilter"), dict) else []
            )
        ]
        if not scoped:
            return None
        if len(scoped) != 1:
            raise FinancePreflightError(
                "run project has multiple billing budgets; reconcile ownership first"
            )
        budget = scoped[0]
        self._validate_budget(
            budget, finance, project_number, billing_account, recovery_project_id
        )
        channel_name = finance["alert_notification_channel"]
        channel_status, channel = self._get(
            "monitoring.googleapis.com",
            f"/v3/{channel_name}",
            recovery_project_id,
        )
        if (
            channel_status != 200 or not isinstance(channel, dict)
            or channel.get("name") != channel_name
            or channel.get("type") != "email"
            or channel.get("enabled") is not True
            or channel.get("verificationStatus") != "VERIFIED"
        ):
            raise FinancePreflightError(
                "reviewed email channel is missing, disabled, or unverified"
            )
        return {
            "project_number": project_number,
            "budget_name": budget["name"],
            "observed_at": at,
            "budget_response_sha256": _digest({
                "budget": budget,
                "notification_channel": channel,
            }),
            "alert_receipt_sha256": finance["alert_delivery_evidence_sha256"],
            "credit_receipt_sha256": finance["credit_evidence_sha256"],
            "verified_by": principal,
        }

    def _validate_budget(
        self, budget, finance, project_number, billing_account, recovery_project_id
    ):
        if not isinstance(budget, dict):
            raise FinancePreflightError("budget resource is malformed")
        name = budget.get("name")
        prefix = f"{billing_account}/budgets/"
        if not isinstance(name, str) or not name.startswith(prefix) or len(name) <= len(prefix):
            raise FinancePreflightError("project budget name is outside the reviewed account")
        filter_value = budget.get("budgetFilter")
        if not isinstance(filter_value, dict) or set(filter_value) - {
            "projects", "creditTypesTreatment", "calendarPeriod"
        }:
            raise FinancePreflightError("project budget contains unreviewed scope filters")
        if filter_value.get("projects") != [f"projects/{project_number}"]:
            raise FinancePreflightError("budget must cover only this exact run project")
        if filter_value.get("creditTypesTreatment") != "EXCLUDE_ALL_CREDITS":
            raise FinancePreflightError("budget must track gross cost before credits")
        if filter_value.get("calendarPeriod") != "MONTH":
            raise FinancePreflightError("run budget must use a monthly period")
        amount = budget.get("amount", {}).get("specifiedAmount")
        if not isinstance(amount, dict) or amount.get("currencyCode") != "GBP":
            raise FinancePreflightError("run budget must use a specified GBP amount")
        try:
            value = (
                Decimal(str(amount.get("units", "0")))
                + Decimal(str(amount.get("nanos", 0))) / Decimal(1_000_000_000)
            )
            threshold = Decimal(str(finance["monthly_alert_threshold_gbp"]))
            estimate = Decimal(str(finance["gross_estimate_gbp"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            raise FinancePreflightError(
                "budget amount or registered estimate is malformed"
            ) from None
        if value != threshold or value < estimate:
            raise FinancePreflightError(
                "budget amount must match the reviewed threshold and cover gross estimate"
            )
        rules = budget.get("thresholdRules")
        normalized = set()
        if not isinstance(rules, list):
            raise FinancePreflightError("budget alert thresholds are missing")
        for rule in rules:
            if not isinstance(rule, dict):
                raise FinancePreflightError("budget alert threshold is malformed")
            percent = rule.get("thresholdPercent")
            basis = rule.get("spendBasis", "CURRENT_SPEND")
            if isinstance(percent, bool) or not isinstance(percent, (int, float)):
                raise FinancePreflightError("budget alert threshold is malformed")
            normalized.add((float(percent), basis))
        if normalized != EXPECTED_THRESHOLDS or len(rules) != len(EXPECTED_THRESHOLDS):
            raise FinancePreflightError(
                "budget must retain the five actual and two forecast thresholds"
            )
        notification = budget.get("notificationsRule")
        channels = (
            notification.get("monitoringNotificationChannels")
            if isinstance(notification, dict) else None
        )
        expected_channel = finance.get("alert_notification_channel")
        if (
            channels != [expected_channel]
            or not isinstance(expected_channel, str)
            or re.fullmatch(
                rf"projects/{re.escape(recovery_project_id)}/notificationChannels/[0-9]{{1,20}}",
                expected_channel,
            ) is None
            or notification.get("disableDefaultIamRecipients") is not True
            or notification.get("enableProjectLevelRecipients") is True
            or notification.get("pubsubTopic")
        ):
            raise FinancePreflightError(
                "budget email route does not match the exact reviewed recovery-project channel"
            )

    def _list_budgets(self, billing_account, quota_project):
        path = f"/v1/{billing_account}/budgets"
        result = []
        token = None
        for _ in range(MAX_PAGES):
            params = {"pageSize": PAGE_SIZE}
            if token:
                params["pageToken"] = token
            status, payload = self._get(
                "billingbudgets.googleapis.com", path, quota_project, params=params
            )
            if status != 200 or not isinstance(payload, dict):
                raise FinancePreflightError("billing budget inventory could not be read completely")
            items = payload.get("budgets", [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise FinancePreflightError("billing budget inventory is malformed")
            result.extend(items)
            token = payload.get("nextPageToken")
            if token is None:
                return result
            if not isinstance(token, str) or not token:
                raise FinancePreflightError("billing budget page token is malformed")
        raise FinancePreflightError("billing budget inventory exceeded the page limit")

    def _get(self, host, path, quota_project, *, params=None):
        if host not in ALLOWED_HOSTS or not path.startswith("/") or "?" in path or "#" in path:
            raise FinancePreflightError("finance preflight request target is invalid")
        token = self._access_token()
        self._principal(token)
        query = urlencode(params or {}, doseq=True)
        url = f"https://{host}{path}" + (f"?{query}" if query else "")
        if urlsplit(url).hostname not in ALLOWED_HOSTS:
            raise FinancePreflightError(
                "finance preflight request escaped the Google API allowlist"
            )
        try:
            response = self._transport.request(
                "GET", url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "x-goog-user-project": quota_project,
                },
                body=None,
                timeout=self._timeout,
                allow_redirects=False,
            )
        except Exception:
            raise FinancePreflightError("finance preflight provider request failed") from None
        if response.status_code == 404:
            return 404, None
        if response.status_code != 200:
            raise FinancePreflightError(
                f"finance preflight provider returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except Exception:
            raise FinancePreflightError(
                "finance preflight provider response was malformed"
            ) from None
        return 200, payload

    def _access_token(self):
        try:
            token = self._token_source.access_token()
        except Exception:
            raise FinancePreflightError(
                "explicit finance operator credential is unavailable"
            ) from None
        if (
            not isinstance(token, str) or not token or token.strip() != token
            or "\n" in token or "\r" in token
        ):
            raise FinancePreflightError("finance operator credential returned an invalid token")
        return token

    def _principal(self, token=None):
        access_token = token or self._access_token()
        verifier = getattr(self._token_source, "verify_principal", None)
        try:
            principal = (
                verifier(access_token) if callable(verifier)
                else verify_access_token_principal(
                    self._identity_transport,
                    access_token,
                    expected_email=self._expected_email,
                )
            )
        except Exception:
            raise FinancePreflightError(
                "finance access token did not verify its Google user"
            ) from None
        if (
            not principal.auditor_id.startswith("google-user-")
            or principal.email != self._expected_email
            or principal.auditor_id != self._expected_subject
        ):
            raise FinancePreflightError(
                "finance access token principal does not match the reviewed operator"
            )
        return principal.auditor_id

    @staticmethod
    def _aware(value):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise FinancePreflightError("finance observation clock must include a timezone")
        return value.astimezone(timezone.utc)
