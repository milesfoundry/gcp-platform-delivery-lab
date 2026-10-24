variable "project_id" {
  description = "Dedicated disposable project for this run. No shared recovery resources."
  type        = string
}
variable "run_id" {
  description = "Unique lowercase run ID used in names and cloud resource labels."
  type        = string
  validation {
    condition     = can(regex("^lab-[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$", var.run_id))
    error_message = "Use a unique lab-prefixed run ID of at most 24 lowercase letters, digits, or hyphens."
  }
}
variable "region" {
  description = "London region for the regional HA lab."
  type        = string
  default     = "europe-west2"
  validation {
    condition     = var.region == "europe-west2"
    error_message = "This profile is restricted to London."
  }
}
variable "zones" {
  description = "Exactly three distinct London worker zones, one node per zone."
  type        = set(string)
  default     = ["europe-west2-a", "europe-west2-b", "europe-west2-c"]
  validation {
    condition     = length(var.zones) == 3 && alltrue([for zone in var.zones : can(regex("^europe-west2-[abc]$", zone))])
    error_message = "Select the three distinct London zones."
  }
}
variable "operator_cidr" {
  description = "Approved operator egress IPv4 CIDR for the Kubernetes API."
  type        = string
  validation {
    condition     = can(cidrnetmask(var.operator_cidr)) && !endswith(var.operator_cidr, "/0")
    error_message = "Provide a restricted IPv4 CIDR; /0 is forbidden."
  }
}
variable "config_sync_version" {
  description = "Explicit supported Config Sync version qualified before a cloud run."
  type        = string
}
variable "source_revision" {
  description = "Reviewed 40-character Git commit for the root configuration."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.source_revision))
    error_message = "Pin an immutable Git commit."
  }
}
variable "deletion_protection" {
  description = "Disable deliberately in a reviewed teardown change, never as a default."
  type        = bool
  default     = true
}
