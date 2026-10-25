terraform {
  backend "gcs" {}
  required_version = ">= 1.10, < 2.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "7.22.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  secrets = {
    ALPACA_API_KEY    = "bagel-alpaca-api-key"
    ALPACA_SECRET_KEY = "bagel-alpaca-secret-key"
    DATABASE_URL      = "bagel-database-url"
  }
  environment = {
    ALPACA_ENDPOINT                = "https://paper-api.alpaca.markets"
    BAGEL_SYMBOLS                  = join(",", var.symbols)
    BAGEL_FEED                     = "iex"
    BAGEL_MAX_HISTORY_YEARS        = "5"
    BAGEL_AUTO_BACKFILL            = "true"
    BAGEL_DATABASE_BUDGET_MB       = "450"
    BAGEL_WATCHLIST_POLL_SECONDS   = "30"
    BAGEL_HISTORICAL_DELAY_MINUTES = "15"
    BAGEL_WINDOW_OPEN              = "04:00"
    BAGEL_WINDOW_CLOSE             = "20:00"
    BAGEL_HEALTH_PORT              = "9090"
  }
}

resource "google_project_service" "api" {
  for_each = toset([
    "run.googleapis.com", "artifactregistry.googleapis.com", "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com", "logging.googleapis.com", "monitoring.googleapis.com",
    "iam.googleapis.com", "cloudresourcemanager.googleapis.com", "storage.googleapis.com"
  ])
  service            = each.key
  disable_on_destroy = false
}

# Bootstrapped once with gcloud, then imported into this remote state.
resource "google_storage_bucket" "state" {
  name                        = "${var.project_id}-tfstate"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  lifecycle { prevent_destroy = true }
}

resource "google_storage_bucket" "build_source" {
  name                        = "${var.project_id}-build-source"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  lifecycle_rule {
    condition { age = 7 }
    action { type = "Delete" }
  }
}

resource "google_service_account" "builder" {
  account_id   = "bagel-build"
  display_name = "Bagel image builds"
}

resource "google_project_iam_member" "build_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_storage_bucket_iam_member" "build_source" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_artifact_registry_repository_iam_member" "builder" {
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "bagel"
  format        = "DOCKER"
  depends_on    = [google_project_service.api]
}

resource "google_service_account" "worker" {
  account_id   = "bagel-market-data"
  display_name = "Bagel market-data ingestion"
}

resource "google_secret_manager_secret" "runtime" {
  for_each  = local.secrets
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.api]
}

resource "google_secret_manager_secret_iam_member" "worker" {
  for_each  = local.secrets
  secret_id = google_secret_manager_secret.runtime[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_cloud_run_v2_worker_pool" "market_data" {
  count               = var.enable_runtime ? 1 : 0
  name                = "bagel-market-data"
  location            = var.region
  deletion_protection = true
  scaling {
    manual_instance_count = var.worker_instances
  }
  template {
    service_account = google_service_account.worker.email
    containers {
      image = var.image
      args  = ["run"]
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
      dynamic "env" {
        for_each = local.environment
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.runtime[env.key].secret_id
              version = var.secret_versions[env.key]
            }
          }
        }
      }
      startup_probe {
        http_get {
          path = "/health/live"
          port = 9090
        }
        period_seconds    = 10
        failure_threshold = 12
      }
      liveness_probe {
        http_get {
          path = "/health/live"
          port = 9090
        }
        period_seconds    = 30
        failure_threshold = 3
      }
    }
  }
  lifecycle {
    precondition {
      condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
      error_message = "Runtime requires an immutable image digest."
    }
  }
  depends_on = [google_project_service.api, google_secret_manager_secret_iam_member.worker]
}

resource "google_cloud_run_v2_job" "historical" {
  for_each            = var.enable_runtime ? toset(["backfill", "reconcile"]) : toset([])
  name                = "bagel-${each.key}"
  location            = var.region
  deletion_protection = true
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.worker.email
      timeout         = "86400s"
      max_retries     = 3
      containers {
        image = var.image
        # Backfill is intentionally dry-run by default. Override args for an approved load.
        args = each.key == "backfill" ? ["backfill", "--dry-run"] : ["reconcile"]
        resources {
          limits = { cpu = "1", memory = "512Mi" }
        }
        dynamic "env" {
          for_each = local.environment
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = local.secrets
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.runtime[env.key].secret_id
                version = var.secret_versions[env.key]
              }
            }
          }
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
      error_message = "Jobs require an immutable image digest."
    }
  }
  depends_on = [google_project_service.api, google_secret_manager_secret_iam_member.worker]
}

resource "google_logging_metric" "write_failures" {
  name   = "bagel/write_failures"
  filter = "resource.type=\"cloud_run_worker_pool\" AND jsonPayload.event=\"batch_write_retry\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [google_project_service.api]
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
output "runtime_service_account" {
  value = google_service_account.worker.email
}
