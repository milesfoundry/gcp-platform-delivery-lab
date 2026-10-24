"""Assemble the GCP project-reclaimer controller with one verified identity.

This is a local runtime composition boundary. It performs no cloud requests
until the returned ledger or controller is used, and it never discovers
ambient credentials or changes cloud resources during construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from gcp_cleanup_adapter import TokenSource, Transport
from gcp_project_lifecycle_adapter import GCPProjectLifecycleAdapter
from gcp_token_identity import CloudRunServiceAccountTokenSource, IdentityTransport
from recovery_controller import RecoveryController
from run_ledger import GcsObjectStore, RunLedger


@dataclass(frozen=True)
class ProjectReclaimerConfig:
    ledger_bucket_name: str
    ledger_project_id: str
    project_id: str
    run_id: str
    parent: str
    billing_account_name: str
    enabled_services: tuple[str, ...]
    expected_email: str
    expected_auditor_id: str
    identity_audience: str | None = None


@dataclass(frozen=True)
class ProjectReclaimerRuntime:
    controller: RecoveryController
    ledger: RunLedger
    provider: GCPProjectLifecycleAdapter


def build_project_reclaimer_runtime(
    config: ProjectReclaimerConfig,
    *,
    token_source: TokenSource,
    transport: Transport,
    identity_transport: IdentityTransport | None,
    clock: Callable | None = None,
) -> ProjectReclaimerRuntime:
    """Wire provider and GCS ledger requests to one principal-bound token source."""
    store = GcsObjectStore(
        config.ledger_bucket_name,
        project_id=config.ledger_project_id,
        token_source=token_source,
        identity_transport=identity_transport,
        expected_email=config.expected_email,
        expected_auditor_id=config.expected_auditor_id,
    )
    ledger = RunLedger(store, clock=clock)
    provider = GCPProjectLifecycleAdapter(
        transport,
        token_source,
        project_id=config.project_id,
        run_id=config.run_id,
        parent=config.parent,
        billing_account_name=config.billing_account_name,
        enabled_services=list(config.enabled_services),
        identity_transport=identity_transport,
        project_reclaimer_email=config.expected_email,
        project_reclaimer_auditor_id=config.expected_auditor_id,
    )
    controller = RecoveryController(ledger, provider, clock=clock)
    return ProjectReclaimerRuntime(
        controller=controller,
        ledger=ledger,
        provider=provider,
    )


def build_cloud_run_project_reclaimer_runtime(
    config: ProjectReclaimerConfig,
    *,
    transport: Transport,
    request,
    clock: Callable | None = None,
) -> ProjectReclaimerRuntime:
    """Build the scheduled runtime from Cloud Run's explicit metadata identity."""
    if config.identity_audience is None:
        raise ValueError("a fixed Cloud Run identity-token audience is required")
    token_source = CloudRunServiceAccountTokenSource.from_metadata_server(
        request=request,
        expected_email=config.expected_email,
        expected_subject=_service_account_subject(config.expected_auditor_id),
        audience=config.identity_audience,
    )
    return build_project_reclaimer_runtime(
        config,
        token_source=token_source,
        transport=transport,
        identity_transport=None,
        clock=clock,
    )


def _service_account_subject(auditor_id: str) -> str:
    prefix = "google-service-account-"
    if not isinstance(auditor_id, str) or not auditor_id.startswith(prefix):
        raise ValueError("scheduled reclaimer must use a service-account principal ID")
    subject = auditor_id.removeprefix(prefix)
    if not subject.isdigit():
        raise ValueError("service-account principal ID must end in its numeric subject")
    return subject
