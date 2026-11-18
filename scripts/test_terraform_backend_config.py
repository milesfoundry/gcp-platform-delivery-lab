"""Checks exact, non-secret Terraform GCS backend configuration."""

import os
from pathlib import Path
import tempfile
import unittest

from terraform_backend_config import (
    BackendConfigError,
    bootstrap_backend_config,
    lab_backend_config,
    render_backend_config,
    write_backend_config,
)
from test_run_contract import intent


class TerraformBackendConfigTests(unittest.TestCase):
    def test_lab_uses_manifest_bucket_and_default_workspace_object_path(self):
        manifest = intent()
        config = lab_backend_config(manifest)
        self.assertEqual(config, {
            "bucket": "run-state-lab-20261018-a1b2",
            "prefix": "runs/lab-20261018-a1b2/terraform",
            "project": "recovery-lab-456",
        })
        self.assertEqual(
            render_backend_config(config),
            'bucket = "run-state-lab-20261018-a1b2"\n'
            'prefix = "runs/lab-20261018-a1b2/terraform"\n',
        )

    def test_lab_rejects_wrong_state_object_and_invalid_manifest(self):
        manifest = intent()
        manifest["terraform_state"]["backend"]["object"] = (
            "runs/lab-20261018-a1b2/terraform/other.tfstate"
        )
        with self.assertRaises(BackendConfigError):
            lab_backend_config(manifest)
        with self.assertRaises(BackendConfigError):
            lab_backend_config({"schema": "wrong"})

    def test_bootstrap_backend_requires_exact_valid_bucket_and_project_ids(self):
        self.assertEqual(
            bootstrap_backend_config("portfolio-bootstrap-state", "recovery-lab-456"),
            {"bucket": "portfolio-bootstrap-state", "prefix": "portfolio/bootstrap",
             "project": "recovery-lab-456"},
        )
        for bucket, project in (
            ("../unsafe", "recovery-lab-456"),
            ("google-bootstrap-state", "recovery-lab-456"),
            ("portfolio-bootstrap-state", "wrong/id"),
        ):
            with self.subTest(bucket=bucket, project=project), self.assertRaises(BackendConfigError):
                bootstrap_backend_config(bucket, project)

    def test_backend_config_file_is_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backend.hcl"
            write_backend_config(output, {
                "bucket": "portfolio-bootstrap-state",
                "prefix": "portfolio/bootstrap",
                "project": "recovery-lab-456",
            })
            self.assertEqual(output.read_text(),
                             'bucket = "portfolio-bootstrap-state"\n'
                             'prefix = "portfolio/bootstrap"\n')
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            with self.assertRaises(BackendConfigError):
                write_backend_config(output, {
                    "bucket": "portfolio-bootstrap-state",
                    "prefix": "portfolio/bootstrap",
                    "project": "recovery-lab-456",
                })

    def test_render_rejects_secrets_and_path_traversal(self):
        with self.assertRaises(BackendConfigError):
            render_backend_config({
                "bucket": "portfolio-bootstrap-state",
                "prefix": "portfolio/bootstrap",
                "project": "recovery-lab-456",
                "credentials": "secret-value",
            })
        with self.assertRaises(BackendConfigError):
            render_backend_config({
                "bucket": "portfolio-bootstrap-state",
                "prefix": "portfolio/../bootstrap",
                "project": "recovery-lab-456",
            })


if __name__ == "__main__":
    unittest.main()
