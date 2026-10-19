variable "recovery_project_id" {
  description = "Persistent recovery project that owns the reaper Job, ledger, and Scheduler controls."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.recovery_project_id))
    error_message = "recovery_project_id must be a valid Google Cloud project ID."
  }
}

variable "run_project_id" {
  description = "Exact disposable project for this run; it must already exist before enabling its retirement role."
  type        = string
  validation {
    condition = (
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.run_project_id)) &&
      var.run_project_id != var.recovery_project_id
    )
    error_message = "run_project_id must be a valid project distinct from the recovery project."
  }
}

variable "run_id" {
  description = "Run ID. Its maximum length reserves room for both deterministic service-account suffixes."
  type        = string
  validation {
    condition     = can(regex("^lab-[a-z0-9]([a-z0-9-]{0,17}[a-z0-9])?$", var.run_id))
    error_message = "run_id must match the bounded lab- format and fit the -reaper and -sched account IDs."
  }
}

variable "region" {
  description = "All per-run controls are restricted to London."
  type        = string
  default     = "europe-west2"
  validation {
    condition     = var.region == "europe-west2"
    error_message = "The portfolio recovery controls must remain in europe-west2 (London)."
  }
}

variable "reaper_image_digest" {
  description = "Reviewed immutable reaper container image reference. Mutable tags are rejected."
  type        = string
  validation {
    condition     = can(regex("^ghcr\\.io/[a-z0-9-]+/gcp-reaper@sha256:[0-9a-f]{64}$", var.reaper_image_digest))
    error_message = "reaper_image_digest must be a GHCR gcp-reaper image pinned by SHA-256 digest."
  }
}

variable "ledger_bucket_name" {
  description = "Existing private recovery bucket containing the exact run manifest object."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$", var.ledger_bucket_name))
    error_message = "ledger_bucket_name must be a valid Cloud Storage bucket name."
  }
}

variable "billing_account_name" {
  description = "Exact billing account used by this run, in billingAccounts/XXXXXX-XXXXXX-XXXXXX form."
  type        = string
  validation {
    condition     = can(regex("^billingAccounts/[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$", var.billing_account_name))
    error_message = "billing_account_name must be a full Google Cloud billing account resource name."
  }
}

variable "run_parent" {
  description = "Exact folder or organization resource that owns the disposable project."
  type        = string
  validation {
    condition     = can(regex("^(folders|organizations)/[0-9]{1,20}$", var.run_parent))
    error_message = "run_parent must be a folder or organization resource name."
  }
}

variable "enabled_services" {
  description = "Sorted unique API allowlist persisted in the run manifest."
  type        = list(string)
  validation {
    condition     = length(var.enabled_services) > 0 && var.enabled_services == sort(distinct(var.enabled_services))
    error_message = "enabled_services must be a non-empty sorted list without duplicates."
  }
}

variable "reaper_email" {
  description = "Exact service-account email corresponding to the per-run reaper identity."
  type        = string
  validation {
    condition     = var.reaper_email == "${var.run_id}-reaper@${var.recovery_project_id}.iam.gserviceaccount.com"
    error_message = "reaper_email must match this run's deterministic reaper service account."
  }
}

variable "identity_audience" {
  description = "Fixed audience used to verify the Cloud Run execution identity token."
  type        = string
  validation {
    condition     = length(var.identity_audience) >= 1 && length(var.identity_audience) <= 512
    error_message = "identity_audience must contain 1 to 512 characters."
  }
}

variable "scheduler_schedule" {
  description = "Recurring expiry poll. It remains active through cleanup and independent closure audit."
  type        = string
  default     = "*/5 * * * *"
  validation {
    condition     = var.scheduler_schedule == "*/5 * * * *"
    error_message = "The recovery trigger must poll every five minutes until audited control reclamation."
  }
}

variable "enable_project_retirement_iam" {
  description = "Create the exact-run custom retirement role only after the disposable project exists."
  type        = bool
  default     = false
}
