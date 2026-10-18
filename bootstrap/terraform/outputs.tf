output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}
output "ci_service_account" {
  value = google_service_account.ci.email
}
output "state_contract" {
  value = {
    backend           = "local"
    lifecycle         = "ephemeral-run-workspace"
    retained          = false
    artifact_upload   = false
    cloud_roles_empty = length(var.approved_project_roles) == 0
  }
}
