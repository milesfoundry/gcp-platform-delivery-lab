output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}
output "ci_service_account" {
  value = google_service_account.ci.email
}
output "state_contract" {
  value = {
    backend         = "gcs"
    bucket_owner    = "externally-provisioned"
    backup_status   = "unqualified"
    artifact_upload = false
  }
}
