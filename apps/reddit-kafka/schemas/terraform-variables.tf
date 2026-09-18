variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "reddit-kafka"
}

variable "environment" {
  description = "Deployment environment (e.g., dev, staging, prod)"
  type        = string
  default     = "production"
}

variable "registry_name" {
  description = "Glue Schema Registry name"
  type        = string
  default     = "reddit-kafka-schemas"
}

variable "registry_description" {
  description = "Schema registry description"
  type        = string
  default     = "Managed by Terraform - Reddit Kafka Schema Registry"
}

variable "schema_name" {
  description = "Name of the Reddit comment schema in AWS Glue."
  type        = string
  default     = "RedditComment"
}

variable "schema_compatibility" {
  description = "Schema compatibility mode. BACKWARD ensures new schemas can read old data."
  type        = string
  default     = "BACKWARD" # Changed from BACKWARD_ALL for better dev velocity
  validation {
    condition = contains([
      "NONE", "DISABLED", "BACKWARD",
      "FORWARD", "BOTH", "BACKWARD_ALL",
      "FORWARD_ALL", "ALL"
    ], var.schema_compatibility)
    error_message = "Invalid compatibility mode. Must be one of: NONE, DISABLED, BACKWARD, FORWARD, BOTH, BACKWARD_ALL, FORWARD_ALL, ALL."
  }
}

variable "tags" {
  description = "Common tags to apply to all resources"
  type        = map(string)
  default = {
    Project   = "reddit-kafka"
    ManagedBy = "terraform"
    Layer     = "data-governance"
  }
}
