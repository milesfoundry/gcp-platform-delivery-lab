terraform {
  required_version = ">= 1.12, < 2.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 8.5.0"
    }
  }

  # Supply the private per-run state bucket and a run-specific prefix through
  # backend configuration. This bucket is provisioned before this root runs.
  backend "gcs" {}
}

provider "google" {
  project = var.recovery_project_id
  region  = var.region
}

provider "google" {
  alias   = "run"
  project = var.run_project_id
  region  = var.region
}
