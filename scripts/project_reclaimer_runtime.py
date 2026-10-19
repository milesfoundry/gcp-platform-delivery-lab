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
from gcp_token_identity import IdentityTransport
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
    identity_transport: IdentityTransport,
    clock: Callable | None = None,
) -> ProjectReclaimerRuntime:
    """Wire provider and GCS ledger requests to one token and human identity."""
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
    )
    controller = RecoveryController(ledger, provider, clock=clock)
    return ProjectReclaimerRuntime(
        controller=controller,
        ledger=ledger,
        provider=provider,
    )
