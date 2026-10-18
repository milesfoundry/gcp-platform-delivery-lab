output "cluster_name" {
  value = google_container_cluster.lab.name
}
output "membership" {
  value = google_gke_hub_membership.lab.id
}
output "run_inventory" {
  description = "Run-owned provider resource identities for the durable manifest."
  value = {
    schema     = "portfolio.gcp-run-inventory.v1"
    run_id     = var.run_id
    project_id = var.project_id
    region     = var.region
    resources = [
      {
        type            = "gcp.compute.network"
        id              = google_compute_network.lab.id
        ownership_proof = "run_name"
      },
      {
        type            = "gcp.compute.subnetwork"
        id              = google_compute_subnetwork.lab.id
        ownership_proof = "run_name"
      },
      {
        type            = "gcp.compute.router"
        id              = google_compute_router.lab.id
        ownership_proof = "run_name"
      },
      {
        type            = "gcp.compute.router_nat"
        id              = google_compute_router_nat.lab.id
        ownership_proof = "run_name"
      },
      {
        type            = "gcp.gke.cluster"
        id              = google_container_cluster.lab.id
        ownership_proof = "run_label"
      },
      {
        type            = "gcp.gke.node_pool"
        id              = google_container_node_pool.lab.id
        ownership_proof = "run_name"
      },
      {
        type            = "gcp.gkehub.membership"
        id              = google_gke_hub_membership.lab.id
        ownership_proof = "run_name"
      },
      {
        type            = "gcp.gkehub.feature_membership"
        id              = google_gke_hub_feature_membership.config_sync.id
        ownership_proof = "run_name"
      },
    ]
  }
}
