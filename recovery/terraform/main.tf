locals {
  reaper_account_id    = "${var.run_id}-reaper"
  scheduler_account_id = "${var.run_id}-sched"
  job_name             = "${var.run_id}-reaper"
  scheduler_job_name   = "${var.run_id}-expiry"
  ledger_object_name   = "runs/${var.run_id}/manifest.json"
  ledger_resource_name = "projects/_/buckets/${var.ledger_bucket_name}/objects/${local.ledger_object_name}"
}

resource "google_project_service" "recovery_apis" {
  for_each = toset([
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
  ])

  project            = var.recovery_project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "reaper" {
  project      = var.recovery_project_id
  account_id   = local.reaper_account_id
  display_name = "Portfolio run ${var.run_id} retirement job"
  description  = "Per-run retirement identity. It cannot create projects or recovery controls."
  depends_on   = [google_project_service.recovery_apis]
}

resource "google_service_account" "scheduler" {
  project      = var.recovery_project_id
  account_id   = local.scheduler_account_id
  display_name = "Portfolio run ${var.run_id} Scheduler invocation"
  description  = "May invoke only the matching per-run Cloud Run Job."
  depends_on   = [google_project_service.recovery_apis]
}

resource "google_project_iam_custom_role" "ledger_reaper" {
  project     = var.recovery_project_id
  role_id     = "portfolioLedgerReaper${replace(var.run_id, "-", "")}"
  title       = "Portfolio ${var.run_id} exact ledger access"
  description = "Read and replace only this run's manifest object."
  permissions = [
    "storage.objects.create",
    "storage.objects.delete",
    "storage.objects.get",
  ]
  stage = "GA"
}

data "google_storage_bucket" "ledger" {
  name    = var.ledger_bucket_name
  project = var.recovery_project_id
}

resource "google_storage_bucket_iam_member" "ledger_reaper" {
  bucket = var.ledger_bucket_name
  role   = google_project_iam_custom_role.ledger_reaper.name
  member = "serviceAccount:${google_service_account.reaper.email}"

  condition {
    title       = "Exact manifest for ${var.run_id}"
    description = "Restrict ledger access to the one durable manifest object."
    expression  = "resource.name == '${local.ledger_resource_name}'"
  }

  lifecycle {
    precondition {
      condition = (
        data.google_storage_bucket.ledger.project == var.recovery_project_id &&
        data.google_storage_bucket.ledger.uniform_bucket_level_access
      )
      error_message = "The ledger bucket must be in the recovery project with uniform bucket-level access enabled."
    }
  }
}

resource "google_cloud_run_v2_job" "reaper" {
  project             = var.recovery_project_id
  name                = local.job_name
  location            = var.region
  deletion_protection = true

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.reaper.email
      timeout         = "90s"
      max_retries     = 0

      containers {
        image = var.reaper_image_digest

        env {
          name  = "PORTFOLIO_RUN_ID"
          value = var.run_id
        }
        env {
          name  = "PORTFOLIO_PROJECT_ID"
          value = var.run_project_id
        }
        env {
          name  = "PORTFOLIO_RECOVERY_PROJECT_ID"
          value = var.recovery_project_id
        }
        env {
          name  = "PORTFOLIO_RUN_PARENT"
          value = var.run_parent
        }
        env {
          name  = "PORTFOLIO_BILLING_ACCOUNT"
          value = var.billing_account_name
        }
        env {
          name  = "PORTFOLIO_LEDGER_BUCKET"
          value = var.ledger_bucket_name
        }
        env {
          name  = "PORTFOLIO_ENABLED_SERVICES"
          value = jsonencode(var.enabled_services)
        }
        env {
          name  = "PORTFOLIO_REAPER_EMAIL"
          value = var.reaper_email
        }
        env {
          name  = "PORTFOLIO_REAPER_SERVICE_ACCOUNT_ID"
          value = google_service_account.reaper.unique_id
        }
        env {
          name  = "PORTFOLIO_IDENTITY_AUDIENCE"
          value = var.identity_audience
        }
      }
    }
  }

  depends_on = [
    google_project_service.recovery_apis,
    google_storage_bucket_iam_member.ledger_reaper,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = var.recovery_project_id
  location = var.region
  name     = google_cloud_run_v2_job.reaper.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "expiry" {
  project          = var.recovery_project_id
  region           = var.region
  name             = local.scheduler_job_name
  description      = "Recurring expiry and recovery poll for ${var.run_id}."
  schedule         = var.scheduler_schedule
  time_zone        = "Europe/London"
  attempt_deadline = "320s"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.recovery_project_id}/locations/${var.region}/jobs/${local.job_name}:run"
    body        = base64encode("{}")

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  retry_config {
    retry_count = 0
  }

  depends_on = [
    google_cloud_run_v2_job_iam_member.scheduler_invoker,
    google_project_service.recovery_apis,
  ]
}

resource "google_project_iam_custom_role" "project_retirement" {
  count       = var.enable_project_retirement_iam ? 1 : 0
  provider    = google.run
  project     = var.run_project_id
  role_id     = "portfolioProjectRetirement"
  title       = "Portfolio ${var.run_id} exact project retirement"
  description = "Unlink this project's billing assignment and delete this project after independent workload audit."
  permissions = [
    "resourcemanager.projects.delete",
    "resourcemanager.projects.deleteBillingAssignment",
    "resourcemanager.projects.get",
  ]
  stage = "GA"
}

resource "google_project_iam_member" "project_retirement" {
  count    = var.enable_project_retirement_iam ? 1 : 0
  provider = google.run
  project  = var.run_project_id
  role     = google_project_iam_custom_role.project_retirement[0].name
  member   = "serviceAccount:${google_service_account.reaper.email}"
}
