mock_provider "google" {}

variables {
  project_id                 = "synthetic-portfolio-test"
  run_id                     = "lab-20261018-test"
  node_service_account_email = "portfolio-lab-nodes@synthetic-portfolio-test.iam.gserviceaccount.com"
  operator_cidr              = "192.0.2.10/32"
  config_sync_version        = "1.0.0"
  source_revision            = "0000000000000000000000000000000000000000"
}

run "regional_three_node_contract" {
  command = plan
  assert {
    condition     = google_container_cluster.lab.location == "europe-west2" && length(google_container_cluster.lab.node_locations) == 3
    error_message = "Regional control plane and three worker zones required."
  }
  assert {
    condition     = google_container_cluster.lab.name == var.run_id && google_container_cluster.lab.resource_labels["lab_run_id"] == var.run_id
    error_message = "GKE cluster identity and resource labels must include the unique run ID."
  }
  assert {
    condition     = google_container_node_pool.lab.node_config[0].service_account == var.node_service_account_email
    error_message = "Ephemeral nodes must use the persistent bootstrap-managed least-privilege identity."
  }
  assert {
    condition     = output.run_inventory.run_id == var.run_id && [for item in output.run_inventory.resources : item.type] == ["gcp.compute.network", "gcp.compute.subnetwork", "gcp.compute.router", "gcp.compute.router_nat", "gcp.gke.cluster", "gcp.gke.node_pool", "gcp.gkehub.membership", "gcp.gkehub.feature_membership"]
    error_message = "The run manifest inventory must enumerate every run-owned resource type under its unique run ID."
  }
  assert {
    condition     = local.name == var.run_id && google_compute_network.lab.name == var.run_id && google_compute_subnetwork.lab.name == var.run_id && google_compute_router.lab.name == var.run_id && google_compute_router_nat.lab.name == var.run_id
    error_message = "Unlabelled network resources must use deterministic run-scoped names."
  }
  assert {
    condition     = google_container_node_pool.lab.node_count == 1 && length(google_container_node_pool.lab.node_locations) == 3 && google_container_node_pool.lab.node_config[0].machine_type == "e2-medium"
    error_message = "Use three total minimum-candidate e2-medium nodes, not nine."
  }
}

run "foreign_region_rejected" {
  command = plan
  variables { region = "us-central1" }
  expect_failures = [var.region]
}

run "unsafe_run_id_rejected" {
  command = plan
  variables { run_id = "lab-bad/../../target" }
  expect_failures = [var.run_id]
}

run "foreign_node_identity_rejected" {
  command = plan
  variables { node_service_account_email = "portfolio-lab-nodes@other-lab-123.iam.gserviceaccount.com" }
  expect_failures = [var.node_service_account_email]
}

run "single_zone_rejected" {
  command = plan
  variables { zones = ["europe-west2-a"] }
  expect_failures = [var.zones]
}
