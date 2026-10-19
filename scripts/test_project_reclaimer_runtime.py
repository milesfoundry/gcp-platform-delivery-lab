"""Runtime composition tests for one GCS and provider caller identity."""

import unittest
from unittest.mock import patch

from project_reclaimer_runtime import (
    ProjectReclaimerConfig,
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
        )
        make_controller.assert_called_once_with(ledger, provider, clock=clock)
        self.assertIs(runtime.controller, controller)
        self.assertIs(runtime.ledger, ledger)
        self.assertIs(runtime.provider, provider)


if __name__ == "__main__":
    unittest.main()
