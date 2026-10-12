locals {
  repository_id = "1402116284"
  owner_id      = "68283664"
  workflow_ref  = "talisman36935/gcp-platform-delivery-lab/.github/workflows/cloud-run.yaml@refs/heads/main"
  lifecycle_events = toset([
    "created", "ready", "expiry-warning", "teardown-started",
    "teardown-passed", "teardown-failed",
  ])
}

resource "google_project_service" "bootstrap" {
  for_each = toset([
    "iam.googleapis.com", "iamcredentials.googleapis.com",
    "sts.googleapis.com", "cloudresourcemanager.googleapis.com",
    "billingbudgets.googleapis.com", "monitoring.googleapis.com",
    "logging.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = true
}

resource "google_service_account" "ci" {
  account_id   = "portfolio-delivery-ci"
  display_name = "Scoped portfolio delivery workflow identity"
  depends_on   = [google_project_service.bootstrap]
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "portfolio-github"
  display_name              = "Portfolio GitHub federation"
  depends_on                = [google_project_service.bootstrap]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "delivery"
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.ref"                 = "assertion.ref"
    "attribute.workflow_ref"        = "assertion.workflow_ref"
    "attribute.event_name"          = "assertion.event_name"
  }
  attribute_condition = join(" && ", [
    "assertion.repository_id == '${local.repository_id}'",
    "assertion.repository_owner_id == '${local.owner_id}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.workflow_ref == '${local.workflow_ref}'",
    "assertion.event_name == 'workflow_dispatch'",
  ])
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}

resource "google_service_account_iam_member" "federation" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${local.repository_id}"
}

resource "google_project_iam_member" "approved_apply_roles" {
  for_each = var.approved_project_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_monitoring_notification_channel" "cost_email" {
  for_each     = var.enable_cost_alerts ? toset(["enabled"]) : toset([])
  project      = var.project_id
  display_name = "Portfolio lab alerts email"
  type         = "email"
  labels       = { email_address = var.cost_alert_email }
  depends_on   = [google_project_service.bootstrap]
  lifecycle {
    precondition {
      condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.cost_alert_email))
      error_message = "Cost alerts require a valid private email input."
    }
  }
}

resource "google_billing_budget" "cost_alerts" {
  for_each        = var.enable_cost_alerts ? toset(["enabled"]) : toset([])
  billing_account = var.billing_account_id
  display_name    = "Ephemeral portfolio lab gross cost"

  budget_filter {
    projects               = ["projects/${var.project_id}"]
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = var.cost_budget_currency
      units         = tostring(floor(var.cost_budget_amount))
      nanos         = floor((var.cost_budget_amount - floor(var.cost_budget_amount)) * 1000000000)
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.25, 0.5, 0.75, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }
  dynamic "threshold_rules" {
    for_each = [0.75, 1.0]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "FORECASTED_SPEND"
    }
  }

  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.cost_email["enabled"].name]
    enable_project_level_recipients  = true
  }

  lifecycle {
    precondition {
      condition = (
        can(regex("^[0-9]{6}-[0-9]{6}-[0-9]{6}$", var.billing_account_id)) &&
        var.cost_budget_currency == "GBP" &&
        var.cost_budget_amount > 0
      )
      error_message = "Enabled alerts require a valid billing account, GBP currency, and positive notification threshold. The threshold is not a spending cap."
    }
  }
}

resource "google_monitoring_alert_policy" "lifecycle_events" {
  for_each     = var.enable_cost_alerts ? local.lifecycle_events : toset([])
  project      = var.project_id
  display_name = "Portfolio lab ${each.key}"
  combiner     = "OR"
  severity     = "WARNING"

  documentation {
    mime_type = "text/markdown"
    content   = "The ${each.key} lifecycle event was emitted for a portfolio lab run. Check the run ID. Treat expiry warnings and teardown failures as urgent."
  }

  conditions {
    display_name = "Lab ${each.key} event"
    condition_matched_log {
      filter = <<-EOT
        resource.type="global"
        logName="projects/${var.project_id}/logs/portfolio-lifecycle"
        jsonPayload.schema="portfolio.lifecycle.v1"
        jsonPayload.event="${each.key}"
      EOT
      label_extractors = {
        run_id = "EXTRACT(jsonPayload.run_id)"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.cost_email["enabled"].name]

  alert_strategy {
    notification_rate_limit { period = "60s" }
    auto_close = "3600s"
  }

  depends_on = [google_project_service.bootstrap]
}
