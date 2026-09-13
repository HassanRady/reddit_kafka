# Reddit Kafka - Development Environment
# This configuration deploys the complete infrastructure to AWS
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.50" }
  }
  backend "s3" {
    bucket         = "reddit-kafka-tf-state-dev"
    key            = "state/dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "reddit-kafka-tf-locks-dev"
    encrypt        = true
  }
}
provider "aws" {
  region = var.aws_region
}
module "reddit_kafka" {
  source = "../../terraform"
  # Core configuration
  aws_region   = var.aws_region
  project_name = var.project_name
  environment  = var.environment
  # VPC Configuration
  vpc_cidr             = var.vpc_cidr
  public_subnet_cidrs  = var.public_subnet_cidrs
  private_subnet_cidrs = var.private_subnet_cidrs
  availability_zones   = var.availability_zones
  # Database Configuration
  rds_instance_class  = var.rds_instance_class
  rds_master_password = var.rds_master_password
  # Cache Configuration
  redis_node_type  = var.redis_node_type
  redis_num_nodes  = var.redis_num_nodes
  redis_auth_token = var.redis_auth_token
  # Container Configuration
  app_image_tag      = var.app_image_tag
  app_container_port = var.app_container_port
  app_desired_count  = var.app_desired_count
  app_cpu            = var.app_cpu
  app_memory         = var.app_memory
  # HTTPS Configuration
  acm_certificate_arn = var.acm_certificate_arn
  # Secrets
  reddit_client_id     = var.reddit_client_id
  reddit_client_secret = var.reddit_client_secret
  reddit_user_agent    = var.reddit_user_agent
  kafka_sasl_username  = var.kafka_sasl_username
  kafka_sasl_password  = var.kafka_sasl_password
  # CloudWatch
  log_retention_days    = var.log_retention_days
  enable_kms_encryption = var.enable_kms_encryption
}
# Output infrastructure endpoints
output "alb_dns" {
  value       = module.reddit_kafka.alb_dns
  description = "Load balancer DNS name (use this to access the app)"
}
output "rds_endpoint" {
  value       = module.reddit_kafka.rds_endpoint
  description = "RDS cluster reader endpoint"
}
output "redis_endpoint" {
  value       = module.reddit_kafka.redis_endpoint
  description = "Redis endpoint"
}
output "ecr_repository_url" {
  value       = module.reddit_kafka.ecr_repository_url
  description = "ECR repository URL - push images here"
}
output "ecs_cluster_name" {
  value       = module.reddit_kafka.ecs_cluster_name
  description = "ECS cluster name"
}
output "private_subnet_ids" {
  value       = module.reddit_kafka.private_subnet_ids
  description = "Private subnet IDs for ECS networking"
}
output "ecs_security_group_id" {
  value       = module.reddit_kafka.ecs_security_group_id
  description = "Security group ID for ECS tasks"
}
output "ecs_task_definition_arn" {
  value       = module.reddit_kafka.ecs_task_definition_arn
  description = "Latest ECS task definition ARN"
}
output "deployment_info" {
  value = {
    environment  = var.environment
    region       = var.aws_region
    project_name = var.project_name
  }
  description = "Deployment information"
}
