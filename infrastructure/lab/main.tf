locals {
  name = var.run_id
}

resource "google_compute_network" "lab" {
  name                    = local.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "lab" {
  name                     = local.name
  network                  = google_compute_network.lab.id
  ip_cidr_range            = "10.40.0.0/24"
  private_ip_google_access = true
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.44.0.0/16"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.45.0.0/20"
  }
}

# Private nodes need explicit egress for public Git/image sources. NAT is billable.
resource "google_compute_router" "lab" {
  name    = local.name
  network = google_compute_network.lab.id
}
resource "google_compute_router_nat" "lab" {
  name                               = local.name
  router                             = google_compute_router.lab.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  subnetwork {
    name                    = google_compute_subnetwork.lab.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
}

resource "google_container_cluster" "lab" {
  name                     = local.name
  location                 = var.region
  node_locations           = sort(tolist(var.zones))
  network                  = google_compute_network.lab.id
  subnetwork               = google_compute_subnetwork.lab.id
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = var.deletion_protection
  networking_mode          = "VPC_NATIVE"
  release_channel { channel = "REGULAR" }
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }
  resource_labels = {
    lab_run_id = var.run_id
  }
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }
  master_authorized_networks_config {
    cidr_blocks {
      cidr_block   = var.operator_cidr
      display_name = "approved-operator"
    }
  }
  network_policy {
    enabled  = true
    provider = "CALICO"
  }
  depends_on = [google_compute_router_nat.lab]
}

resource "google_container_node_pool" "lab" {
  name           = "lab"
  cluster        = google_container_cluster.lab.id
  location       = var.region
  node_locations = sort(tolist(var.zones))
  node_count     = 1
  management {
    auto_repair  = true
    auto_upgrade = true
  }
  upgrade_settings {
    max_surge       = 0
    max_unavailable = 1
  }
  node_config {
    machine_type    = "e2-medium"
    disk_size_gb    = 30
    disk_type       = "pd-standard"
    service_account = var.node_service_account_email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config { mode = "GKE_METADATA" }
    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }
}

resource "google_gke_hub_membership" "lab" {
  membership_id = local.name
  location      = "global"
  endpoint {
    gke_cluster {
      resource_link = "//container.googleapis.com/${google_container_cluster.lab.id}"
    }
  }
}
resource "google_gke_hub_feature_membership" "config_sync" {
  location   = "global"
  feature    = "configmanagement"
  membership = google_gke_hub_membership.lab.membership_id
  configmanagement {
    version = var.config_sync_version
    config_sync {
      enabled       = true
      source_format = "unstructured"
      prevent_drift = false
      git {
        sync_repo   = "https://github.com/milesfoundry/gcp-platform-delivery-lab"
        sync_branch = "main"
        sync_rev    = var.source_revision
        policy_dir  = "gitops/platform"
        secret_type = "none"
      }
    }
  }
  depends_on = [google_container_node_pool.lab]
}
