variable "aws_region" {
  type    = string
  default = "us-east-1"
}
variable "project_name" {
  type    = string
  default = "reddit-kafka"
}
variable "environment" {
  type    = string
  default = "dev"
}
variable "vpc_cidr" {
  type    = string
  default = "10.30.0.0/16"
}
variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.30.0.0/24", "10.30.1.0/24"]
}
variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.30.10.0/24", "10.30.11.0/24"]
}
variable "availability_zones" {
  type    = list(string)
  default = []
}
variable "rds_instance_class" {
  type    = string
  default = "db.r5.large"
}
variable "rds_master_password" {
  type      = string
  sensitive = true
  default   = ""
}
variable "redis_node_type" {
  type    = string
  default = "cache.t3.micro"
}
variable "redis_num_nodes" {
  type    = number
  default = 2
}
variable "redis_auth_token" {
  type      = string
  sensitive = true
  default   = ""
}
variable "app_image_tag" {
  type    = string
  default = "latest"
}
variable "app_container_port" {
  type    = number
  default = 8000
}
variable "app_desired_count" {
  type    = number
  default = 2
}
variable "app_cpu" {
  type    = number
  default = 512
}
variable "app_memory" {
  type    = number
  default = 1024
}
variable "acm_certificate_arn" {
  type    = string
  default = ""
}
variable "reddit_client_id" {
  type      = string
  sensitive = true
  default   = ""
}
variable "reddit_client_secret" {
  type      = string
  sensitive = true
  default   = ""
}
variable "reddit_user_agent" {
  type    = string
  default = "ubuntu:StreamAnalysis:v1.0.0 (by u/HassanRady)"
}
variable "kafka_sasl_username" {
  type    = string
  default = "reddit-kafka"
}
variable "kafka_sasl_password" {
  type      = string
  sensitive = true
  default   = ""
}
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "enable_kms_encryption" {
  type    = bool
  default = true
}

variable "kafka_security_protocol" {
  type    = string
  default = "SASL_SSL"
}



variable "postgres_db_name" {
  type    = string
  default = "reddit_stream"

}
variable "postgres_user" {
  type    = string
  default = "postgres"
}
variable "redis_user" {
  type    = string
  default = "default"
}
variable "kafka_raw_text_topic" {
  type    = string
  default = "RedditComment"
}



variable "schema_registry_name" {
  type    = string
  default = "reddit-kafka-schemas"
}

variable "schema_name" {
  type    = string
  default = "RedditComment"
}

variable "schema_version" {
  type    = string
  default = "1"
}

variable "use_localstack" {
  type    = bool
  default = false
}

variable "db_flush_interval" {
  type    = string
  default = "10"
}

variable "dead_stream_cleanup_interval" {
  type    = string
  default = "120"
}

variable "log_level" {
  type    = string
  default = "INFO"
}