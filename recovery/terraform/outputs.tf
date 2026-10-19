output "reaper_job_name" {
  value = google_cloud_run_v2_job.reaper.name
}

output "scheduler_job_name" {
  value = google_cloud_scheduler_job.expiry.name
}

output "reaper_service_account" {
  value = google_service_account.reaper.email
}

output "scheduler_service_account" {
  value = google_service_account.scheduler.email
}

output "ledger_object_name" {
  value = local.ledger_object_name
}

output "project_retirement_iam_enabled" {
  value = var.enable_project_retirement_iam
}
