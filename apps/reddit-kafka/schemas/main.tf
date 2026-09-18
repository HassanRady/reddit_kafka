provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

resource "aws_glue_registry" "reddit_kafka" {
  registry_name = var.registry_name
  description   = var.registry_description

  tags = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
  })
}

resource "aws_glue_schema" "reddit_comment" {
  registry_arn = aws_glue_registry.reddit_kafka.arn

  schema_name       = var.schema_name
  data_format       = "AVRO"
  compatibility     = var.schema_compatibility
  description       = "Reddit comment messages for raw_text Kafka topic"
  schema_definition = file("${path.module}/reddit_comment.avsc")

  tags = merge(var.tags, {
    SchemaType = var.schema_name
  })
}

resource "aws_cloudwatch_log_group" "schema_registry" {
  name              = "/aws/glue/schema-registry/${var.registry_name}"
  retention_in_days = 7

  tags = var.tags
}

# 4. IAM Policy for Producers/Consumers
# Attach this policy to the IAM Role of your Kafka Producers (ECS, Lambda, EC2)
resource "aws_iam_policy" "glue_schema_access" {
  name        = "${var.registry_name}-access-policy"
  description = "Allows applications to interact with the Glue Schema Registry"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RegistryAccess"
        Effect = "Allow"
        Action = [
          "glue:GetSchemaVersion"
        ]
        Resource = [
          aws_glue_registry.reddit_kafka.arn,
          aws_glue_schema.reddit_comment.arn
        ]
      }
    ]
  })
}


output "registry_arn" {
  description = "ARN of the Glue Schema Registry"
  value       = aws_glue_registry.reddit_kafka.arn
}

output "schema_arn" {
  description = "ARN of the RedditComment schema"
  value       = aws_glue_schema.reddit_comment.arn
}

output "iam_policy_arn" {
  description = "ARN of the IAM policy to be attached to Kafka clients"
  value       = aws_iam_policy.glue_schema_access.arn
}
