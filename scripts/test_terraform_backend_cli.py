"""Actual-CLI regression checks for provider-free Terraform backend HCL."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

import terraform_backend_config


BUCKET = "portfolio-bootstrap-state"
PROJECT = "recovery-lab-456"
PREFIX = "portfolio/bootstrap"


class TerraformBackendCliTests(unittest.TestCase):
    def test_bootstrap_cli_writes_private_bucket_prefix_only_config(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backend.hcl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = terraform_backend_config.main([
                    "bootstrap", "--bucket", BUCKET,
                    "--project-id", PROJECT, "--output", str(output),
                ])
            self.assertEqual((code, stderr.getvalue()), (0, ""))
            self.assertEqual(json.loads(stdout.getvalue()), {
                "kind": "bootstrap", "config_written": True,
            })
            self.assertNotIn(PROJECT, stdout.getvalue())
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                f'bucket = "{BUCKET}"\nprefix = "{PREFIX}"\n',
            )
            self.assertNotIn(PROJECT, output.read_text(encoding="utf-8"))
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

    def test_bootstrap_cli_refuses_existing_output_without_changing_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backend.hcl"
            argv = [
                "bootstrap", "--bucket", BUCKET,
                "--project-id", PROJECT, "--output", str(output),
            ]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(terraform_backend_config.main(argv), 0)
            original = output.read_bytes()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    terraform_backend_config.main(argv)
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(output.read_bytes(), original)

    def test_bootstrap_cli_rejects_invalid_identity_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backend.hcl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    terraform_backend_config.main([
                        "bootstrap", "--bucket", "google-bootstrap-state",
                        "--project-id", PROJECT, "--output", str(output),
                    ])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertFalse(output.exists())

    def test_lab_cli_rejects_unreadable_intent_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backend.hcl"
            missing = Path(directory) / "missing-intent.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    terraform_backend_config.main([
                        "lab", "--intent", str(missing), "--output", str(output),
                    ])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
