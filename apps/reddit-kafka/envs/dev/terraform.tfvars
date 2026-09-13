# Development Environment Configuration
# Copy this file to terraform.tfvars and customize for your environment
# AWS Configuration
aws_region   = "us-east-1"
project_name = "reddit-kafka"
environment  = "dev"
# VPC Configuration
vpc_cidr             = "10.30.0.0/16"
public_subnet_cidrs  = ["10.30.0.0/24", "10.30.1.0/24"]
private_subnet_cidrs = ["10.30.10.0/24", "10.30.11.0/24"]
availability_zones   = [] # Leave empty to auto-detect
# Database Configuration
rds_instance_class  = "db.r5.large" # Supported for Aurora PostgreSQL 16.13 in dev
rds_master_password = ""            # Leave blank to generate a strong password
# Cache Configuration
redis_node_type  = "cache.t3.micro" # Use cache.r6g.large for production
redis_num_nodes  = 2
redis_auth_token = "" # Leave blank to generate a strong auth token
# Application Configuration
app_image_tag      = "latest"
app_container_port = 8000
app_desired_count  = 2
app_cpu            = 512
app_memory         = 1024
# HTTPS Certificate
acm_certificate_arn = "" # Leave empty to use self-signed cert for dev
reddit_user_agent   = "ubuntu:StreamAnalysis:v1.0.0 (by u/HassanRady)"
# Kafka SCRAM Credentials
kafka_sasl_username = "reddit-kafka"
kafka_sasl_password = "" # Leave blank to generate a strong password
# CloudWatch Configuration
log_retention_days    = 30
enable_kms_encryption = true
# Notes:
# - Replace reddit_client_id and reddit_client_secret with your actual credentials
# - Use stronger RDS password in production
# - For production, provide acm_certificate_arn (request from AWS Certificate Manager)
# - For production, set app_desired_count to 3 or more for HA
# - For production, use larger instance classes (db.r6g.large for RDS, cache.r6g.large for Redis)


postgres_db_name     = "reddit_kafka_db"
postgres_user        = "reddit_kafka_user"
redis_user           = "default" # Redis uses "default" user for authentication
kafka_raw_text_topic = "raw-text"

schema_registry_name         = "reddit-kafka-schemas"
schema_name                  = "RedditComment"
schema_version               = "1"
use_localstack               = false
db_flush_interval            = "90"
dead_stream_cleanup_interval = "100"