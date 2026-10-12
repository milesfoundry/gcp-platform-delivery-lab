mock_provider "google" {}

variables {
  project_id = "synthetic-portfolio-test"
  region     = "europe-west2"
}

run "bootstrap_has_no_default_cloud_apply_roles" {
  command = plan
  assert {
    condition     = length(google_project_iam_member.approved_apply_roles) == 0
    error_message = "Bootstrap must not silently grant project apply privileges."
  }
  assert {
    condition     = output.state_contract.backend == "local" && output.state_contract.retained == false && output.state_contract.artifact_upload == false
    error_message = "Terraform state must be local to an ephemeral run workspace and never uploaded."
  }
  assert {
    condition     = length(google_billing_budget.cost_alerts) == 0 && length(google_monitoring_alert_policy.lifecycle_events) == 0
    error_message = "Cost alerts must be explicitly enabled with private billing inputs."
  }
}

run "cost_alerts_cover_early_actual_and_forecast_thresholds" {
  command = plan
  variables {
    enable_cost_alerts   = true
    billing_account_id   = "000000-000000-000000"
    cost_budget_currency = "GBP"
    cost_budget_amount   = 5
    cost_alert_email     = "gcp-alert@example.invalid"
  }
  assert {
    condition     = length(google_billing_budget.cost_alerts["enabled"].threshold_rules) == 7
    error_message = "Budget requires five actual thresholds and two forecast warnings."
  }
  assert {
    condition     = length(google_monitoring_alert_policy.lifecycle_events) == 6 && can(regex("teardown-failed", google_monitoring_alert_policy.lifecycle_events["teardown-failed"].conditions[0].condition_matched_log[0].filter))
    error_message = "Lifecycle email alerts must separately cover all six events, including teardown failure."
  }
  assert {
    condition     = google_billing_budget.cost_alerts["enabled"].budget_filter[0].credit_types_treatment == "EXCLUDE_ALL_CREDITS"
    error_message = "Alert against gross cost before trial credits."
  }
  assert {
    condition     = google_monitoring_notification_channel.cost_email["enabled"].type == "email"
    error_message = "Budget alerts must use the private email notification channel."
  }
}

run "cost_alerts_accept_threshold_above_planning_reference" {
  command = plan
  variables {
    enable_cost_alerts   = true
    billing_account_id   = "000000-000000-000000"
    cost_budget_currency = "GBP"
    cost_budget_amount   = 25
    cost_alert_email     = "gcp-alert@example.invalid"
  }
  assert {
    condition     = google_billing_budget.cost_alerts["enabled"].amount[0].specified_amount[0].units == "25"
    error_message = "The monthly alert threshold is configurable above the planning reference; it is not a spend cap."
  }
}

run "cost_alerts_reject_nonpositive_threshold" {
  command = plan
  variables {
    enable_cost_alerts   = true
    billing_account_id   = "000000-000000-000000"
    cost_budget_currency = "GBP"
    cost_budget_amount   = 0
    cost_alert_email     = "gcp-alert@example.invalid"
  }
  expect_failures = [google_billing_budget.cost_alerts]
}

run "broad_editor_role_is_rejected" {
  command = plan
  variables { approved_project_roles = ["roles/editor"] }
  expect_failures = [var.approved_project_roles]
}
