variable "project_id" {
  type        = string
  description = "Existing GCP project ID. No project or billing account is created."
}
variable "region" {
  type    = string
  default = "us-east4"
}
variable "symbols" {
  type    = list(string)
  default = ["SPY", "QQQ", "IWM", "VXUS"]
  validation {
    condition     = length(var.symbols) > 0 && length(var.symbols) <= 30
    error_message = "Configure 1–30 symbols for the IEX Basic feed."
  }
}
variable "image" {
  type        = string
  default     = ""
  description = "Artifact Registry image at an immutable sha256 digest."
}
variable "enable_runtime" {
  type        = bool
  default     = false
  description = "Keep false until the database, secrets, image, and rollout are approved."
}
variable "worker_instances" {
  type        = number
  default     = 1
  description = "Set to zero for a controlled pause before replacing the singleton worker."
  validation {
    condition     = contains([0, 1], var.worker_instances)
    error_message = "The collector supports zero (paused) or one stream owner."
  }
}
variable "secret_versions" {
  type = map(string)
  default = {
    ALPACA_API_KEY    = "1"
    ALPACA_SECRET_KEY = "1"
    DATABASE_URL      = "1"
  }
  description = "Explicit Secret Manager versions; secret contents are never stored in Terraform."
}
