locals {
  name = "portfolio-delivery"
  apis = toset([
    "compute.googleapis.com", "container.googleapis.com",
    "gkehub.googleapis.com", "anthosconfigmanagement.googleapis.com",
    "gkeconnect.googleapis.com",
    "storage.googleapis.com", "pubsub.googleapis.com",
  ])
}

resource "google_project_service" "lab" {
  for_each           = local.apis
  service            = each.value
  disable_on_destroy = true
}

resource "google_compute_network" "lab" {
  name                    = local.name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.lab]
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

resource "google_service_account" "nodes" {
  account_id   = "portfolio-gke-nodes"
  display_name = "Portfolio GKE nodes; no application cloud permissions"
  depends_on   = [google_project_service.lab]
}
resource "google_project_iam_member" "node_service" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.nodes.email}"
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
  depends_on = [google_compute_router_nat.lab, google_project_iam_member.node_service]
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
    service_account = google_service_account.nodes.email
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
resource "google_gke_hub_feature" "config_sync" {
  name       = "configmanagement"
  location   = "global"
  depends_on = [google_project_service.lab]
}
resource "google_gke_hub_feature_membership" "config_sync" {
  location   = "global"
  feature    = google_gke_hub_feature.config_sync.name
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
