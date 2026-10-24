"""Static safety checks for the Terraform state lifetime contract."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StateContractTests(unittest.TestCase):
    def test_bootstrap_does_not_create_its_own_state_bucket_or_writer(self):
        main = (ROOT / "bootstrap/terraform/main.tf").read_text()
        variables = (ROOT / "bootstrap/terraform/variables.tf").read_text()
        outputs = (ROOT / "bootstrap/terraform/outputs.tf").read_text()
        self.assertNotIn('resource "google_storage_bucket" "state"', main)
        self.assertNotIn('"google_storage_bucket_iam_member" "state_writer', main)
        self.assertNotIn('variable "state_bucket_name"', variables)
        self.assertNotIn("state_bucket", outputs)
        self.assertIn('backend         = "gcs"', outputs)
        self.assertIn('backup_status   = "unqualified"', outputs)
        self.assertIn("artifact_upload = false", outputs)

    def test_both_roots_store_state_under_ignored_terraform_directory(self):
        lab = (ROOT / "infrastructure/lab/backend.tf").read_text()
        bootstrap = (ROOT / "bootstrap/terraform/backend.tf").read_text()
        ignore = (ROOT / ".gitignore").read_text()
        self.assertIn('backend "gcs" {}', lab)
        self.assertIn('backend "gcs" {}', bootstrap)
        self.assertNotIn("credentials", lab)
        self.assertNotIn("credentials", bootstrap)
        self.assertIn(".terraform/", ignore)
        self.assertIn("*.tfstate", ignore)


if __name__ == "__main__":
    unittest.main()
