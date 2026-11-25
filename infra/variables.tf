variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "project" {
  type    = string
  default = "event-pipeline"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "owner" {
  type = string
}

variable "alert_email" {
  type = string
}

variable "budget_limit_usd" {
  type    = string
  default = "1"
}

variable "object_retention_days" {
  type    = number
  default = 3
}

variable "worker_reserved_concurrency" {
  type    = number
  default = 5
}