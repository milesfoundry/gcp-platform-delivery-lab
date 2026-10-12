variable "project_id" {
  description = "Approved dedicated existing lab project."
  type        = string
}
variable "region" {
  description = "Approved region for the temporary GCP lab."
  type        = string
}
variable "approved_project_roles" {
  description = "Separately reviewed cloud apply roles. Empty grants no project roles."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for role in var.approved_project_roles : startswith(role, "roles/") && !contains(["roles/owner", "roles/editor"], role)])
    error_message = "Use explicitly reviewed predefined roles; Owner and Editor are forbidden."
  }
}

variable "enable_cost_alerts" {
  description = "Create the project gross-cost budget, private email channel and lifecycle log alert."
  type        = bool
  default     = false
}

variable "billing_account_id" {
  description = "Approved billing account for the project-scoped budget. Required when cost alerts are enabled."
  type        = string
  default     = ""
}

variable "cost_budget_currency" {
  description = "Must be GBP so the monthly gross-cost warning threshold matches the approved currency."
  type        = string
  default     = ""
}

variable "cost_budget_amount" {
  description = "Monthly cost-notification threshold in GBP; positive and selected for account usage/runway. This is not a spending cap."
  type        = number
  default     = 0
}

variable "cost_alert_email" {
  description = "Private recipient for project budget notifications; keep tfvars/state private."
  type        = string
  sensitive   = true
  default     = ""
}
