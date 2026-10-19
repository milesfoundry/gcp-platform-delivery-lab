"""Runtime composition tests for one GCS and provider caller identity."""

import unittest
from unittest.mock import patch

from project_reclaimer_runtime import (
    ProjectReclaimerConfig,
    build_cloud_run_project_reclaimer_runtime,
    build_project_reclaimer_runtime,
)


class ProjectReclaimerRuntimeTests(unittest.TestCase):
    def test_runtime_passes_one_token_and_identity_transport_to_both_adapters(self):
        config = ProjectReclaimerConfig(
            ledger_bucket_name="private-ledger",
            ledger_project_id="recovery-project-123",
            project_id="portfolio-lab-12345",
            run_id="lab-run-123",
            parent="folders/123456",
            billing_account_name="billingAccounts/ABCDEF-123456-GHIJKL",
            enabled_services=("compute.googleapis.com",),
            expected_email="project-reclaimer@example.com",
            expected_auditor_id="google-user-12345678901234567890",
        )
        token_source = object()
        transport = object()
        identity_transport = object()
        clock = object()
        store = object()
        ledger = object()
        provider = object()
        controller = object()

        with (
            patch("project_reclaimer_runtime.GcsObjectStore", return_value=store) as make_store,
            patch("project_reclaimer_runtime.RunLedger", return_value=ledger) as make_ledger,
            patch(
                "project_reclaimer_runtime.GCPProjectLifecycleAdapter",
                return_value=provider,
            ) as make_provider,
            patch(
                "project_reclaimer_runtime.RecoveryController",
                return_value=controller,
            ) as make_controller,
        ):
            runtime = build_project_reclaimer_runtime(
                config,
                token_source=token_source,
                transport=transport,
                identity_transport=identity_transport,
                clock=clock,
            )

        make_store.assert_called_once_with(
            "private-ledger",
            project_id="recovery-project-123",
            token_source=token_source,
            identity_transport=identity_transport,
            expected_email="project-reclaimer@example.com",
            expected_auditor_id="google-user-12345678901234567890",
        )
        make_ledger.assert_called_once_with(store, clock=clock)
        make_provider.assert_called_once_with(
            transport,
            token_source,
            project_id="portfolio-lab-12345",
            run_id="lab-run-123",
            parent="folders/123456",
            billing_account_name="billingAccounts/ABCDEF-123456-GHIJKL",
            enabled_services=["compute.googleapis.com"],
            identity_transport=identity_transport,
            project_reclaimer_email="project-reclaimer@example.com",
            project_reclaimer_auditor_id="google-user-12345678901234567890",
        )
        make_controller.assert_called_once_with(ledger, provider, clock=clock)
        self.assertIs(runtime.controller, controller)
        self.assertIs(runtime.ledger, ledger)
        self.assertIs(runtime.provider, provider)

    def test_cloud_run_runtime_constructs_one_metadata_identity_source(self):
        config = ProjectReclaimerConfig(
            ledger_bucket_name="private-ledger",
            ledger_project_id="recovery-project-123",
            project_id="portfolio-lab-12345",
            run_id="lab-run-123",
            parent="folders/123456",
            billing_account_name="billingAccounts/ABCDEF-123456-GHIJKL",
            enabled_services=("compute.googleapis.com",),
            expected_email="janitor@recovery-project.iam.gserviceaccount.com",
            expected_auditor_id="google-service-account-12345678901234567890",
            identity_audience="portfolio-reclaimer-v1",
        )
        token_source = object()
        request = object()
        transport = object()
        store, ledger, provider, controller = object(), object(), object(), object()
        with (
            patch(
                "project_reclaimer_runtime.CloudRunServiceAccountTokenSource.from_metadata_server",
                return_value=token_source,
            ) as make_source,
            patch("project_reclaimer_runtime.GcsObjectStore", return_value=store) as make_store,
            patch("project_reclaimer_runtime.RunLedger", return_value=ledger) as make_ledger,
            patch(
                "project_reclaimer_runtime.GCPProjectLifecycleAdapter",
                return_value=provider,
            ) as make_provider,
            patch(
                "project_reclaimer_runtime.RecoveryController",
                return_value=controller,
            ) as make_controller,
        ):
            runtime = build_cloud_run_project_reclaimer_runtime(
                config, transport=transport, request=request
            )

        make_source.assert_called_once_with(
            request=request,
            expected_email=config.expected_email,
            expected_subject="12345678901234567890",
            audience="portfolio-reclaimer-v1",
        )
        make_store.assert_called_once_with(
            "private-ledger",
            project_id="recovery-project-123",
            token_source=token_source,
            identity_transport=None,
            expected_email=config.expected_email,
            expected_auditor_id=config.expected_auditor_id,
        )
        make_provider.assert_called_once_with(
            transport,
            token_source,
            project_id="portfolio-lab-12345",
            run_id="lab-run-123",
            parent="folders/123456",
            billing_account_name="billingAccounts/ABCDEF-123456-GHIJKL",
            enabled_services=["compute.googleapis.com"],
            identity_transport=None,
            project_reclaimer_email=config.expected_email,
            project_reclaimer_auditor_id=config.expected_auditor_id,
        )
        make_ledger.assert_called_once_with(store, clock=None)
        make_controller.assert_called_once_with(ledger, provider, clock=None)
        self.assertIs(runtime.controller, controller)


if __name__ == "__main__":
    unittest.main()
