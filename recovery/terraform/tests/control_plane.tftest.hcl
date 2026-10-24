mock_provider "google" {
  override_during = plan
  mock_data "google_storage_bucket" {
    defaults = {
      project                     = "recovery-lab-456"
      uniform_bucket_level_access = true
      public_access_prevention    = "enforced"
    }
  }
}

mock_provider "google" {
  alias = "run"
}

variables {
  recovery_project_id  = "recovery-lab-456"
  run_project_id       = "sample-lab-123"
  run_id               = "lab-20261018-a1b2"
  reaper_image_digest  = "ghcr.io/milesfoundry/gcp-reaper@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  ledger_bucket_name   = "portfolio-ledger-private"
  billing_account_name = "billingAccounts/ABCDEF-123456-GHIJKL"
  run_parent           = "folders/123456"
  enabled_services     = ["cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com"]
  reaper_email         = "lab-20261018-a1b2-reaper@recovery-lab-456.iam.gserviceaccount.com"
  identity_audience    = "portfolio-reaper-v1"
}

run "controls_are_exact_and_recur_before_retirement_iam" {
  command = plan

  assert {
    condition     = google_cloud_run_v2_job.reaper.name == "lab-20261018-a1b2-reaper"
    error_message = "The per-run reaper Job must use the exact deterministic run name."
  }

  assert {
    condition     = google_service_account.scheduler.account_id == "lab-20261018-a1b2-sched"
    error_message = "The per-run Scheduler identity must fit Google's account-ID limit."
  }

  assert {
    condition     = google_cloud_scheduler_job.expiry.schedule == "*/5 * * * *" && google_cloud_scheduler_job.expiry.retry_config[0].retry_count == 0
    error_message = "Scheduler must poll every five minutes and rely on later polls for retries."
  }

  assert {
    condition     = google_cloud_run_v2_job.reaper.template[0].task_count == 1 && google_cloud_run_v2_job.reaper.template[0].parallelism == 1 && google_cloud_run_v2_job.reaper.template[0].template[0].max_retries == 0
    error_message = "Reaper execution must be single-task, single-parallel, and rely on recurrence for retries."
  }

  assert {
    condition     = google_cloud_run_v2_job.reaper.template[0].template[0].timeout == "90s"
    error_message = "The Job must finish within its two-minute manifest lease."
  }

  assert {
    condition = toset(google_project_iam_custom_role.ledger_reaper.permissions) == toset([
      "storage.objects.create",
      "storage.objects.delete",
      "storage.objects.get",
    ])
    error_message = "The ledger role must not include list or any permission beyond exact-object replacement."
  }

  assert {
    condition     = google_storage_bucket_iam_member.ledger_reaper.condition[0].expression == "resource.name == 'projects/_/buckets/portfolio-ledger-private/objects/runs/lab-20261018-a1b2/manifest.json'"
    error_message = "The reaper ledger IAM condition must name exactly one run manifest object."
  }

  assert {
    condition     = google_cloud_run_v2_job_iam_member.scheduler_invoker.role == "roles/run.invoker"
    error_message = "Scheduler must receive only the Run invoker role on its matching Job."
  }

  assert {
    condition     = length(google_project_iam_custom_role.project_retirement) == 0
    error_message = "The target-project retirement role must stay disabled until that project exists."
  }
}

run "retirement_role_is_confined_to_the_run_project" {
  command = plan

  override_resource {
    target          = google_service_account.reaper
    override_during = plan
    values = {
      email = "lab-20261018-a1b2-reaper@recovery-lab-456.iam.gserviceaccount.com"
    }
  }

  variables {
    enable_project_retirement_iam = true
  }

  assert {
    condition     = google_project_iam_custom_role.project_retirement[0].project == "sample-lab-123"
    error_message = "The retirement role must be defined in the disposable project."
  }

  assert {
    condition = toset(google_project_iam_custom_role.project_retirement[0].permissions) == toset([
      "resourcemanager.projects.delete",
      "resourcemanager.projects.deleteBillingAssignment",
      "resourcemanager.projects.get",
    ])
    error_message = "The disposable-project role must contain only exact project read, billing unlink, and project delete permissions."
  }

  assert {
    condition     = google_project_iam_member.project_retirement[0].member == "serviceAccount:lab-20261018-a1b2-reaper@recovery-lab-456.iam.gserviceaccount.com"
    error_message = "Only the matching per-run reaper identity may receive project retirement permissions."
  }
}

run "rejects_ledger_bucket_without_uniform_access" {
  command = plan

  override_data {
    target = data.google_storage_bucket.ledger
    values = {
      project                     = "recovery-lab-456"
      uniform_bucket_level_access = false
      public_access_prevention    = "enforced"
    }
  }

  expect_failures = [google_storage_bucket_iam_member.ledger_reaper]
}

run "rejects_ledger_bucket_without_public_access_prevention" {
  command = plan

  override_data {
    target = data.google_storage_bucket.ledger
    values = {
      project                     = "recovery-lab-456"
      uniform_bucket_level_access = true
      public_access_prevention    = "inherited"
    }
  }

  expect_failures = [google_storage_bucket_iam_member.ledger_reaper]
}

run "maximum_run_id_fits_service_account_names" {
  command = plan

  variables {
    run_id       = "lab-1234567890123456789"
    reaper_email = "lab-1234567890123456789-reaper@recovery-lab-456.iam.gserviceaccount.com"
  }

  assert {
    condition     = length(google_service_account.reaper.account_id) == 30
    error_message = "The longest allowed run ID plus the -reaper suffix must fit the 30-character account limit."
  }

  assert {
    condition     = length(google_service_account.scheduler.account_id) <= 30
    error_message = "The longest allowed run ID plus the -sched suffix must fit the 30-character account limit."
  }
}

run "rejects_mutable_image_references" {
  command = plan

  variables {
    reaper_image_digest = "ghcr.io/milesfoundry/gcp-reaper:latest"
  }

  expect_failures = [var.reaper_image_digest]
}
