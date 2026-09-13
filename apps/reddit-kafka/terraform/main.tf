
data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "kms" {
  statement {
    sid     = "AllowAccountRoot"
    effect  = "Allow"
    actions = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid     = "AllowSecretsManager"
    effect  = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey*",
      "kms:DescribeKey"
    ]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["secretsmanager.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "random_password" "rds_master" {
  length  = 32
  special = true
  override_special = "!#$%&'()*+,-.:;<=>?[]^_`{|}~"
}

resource "random_password" "redis_auth" {
  length  = 32
  special = false # ElastiCache requires alphanumeric only
  upper   = true
  lower   = true
  numeric = true
}

resource "random_password" "kafka_sasl" {
  length  = 32
  special = true
}

resource "aws_kms_key" "main" {
  count                   = var.enable_kms_encryption ? 1 : 0
  description             = "KMS key for ${local.name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.kms.json
}
resource "aws_kms_alias" "main" {
  count         = var.enable_kms_encryption ? 1 : 0
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.main[0].key_id
}
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = local.name }
}
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}
resource "aws_subnet" "public" {
  count                   = length(var.public_subnet_cidrs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index % length(local.azs)]
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name}-public-${count.index}" }
}
resource "aws_subnet" "private" {
  count             = length(var.private_subnet_cidrs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index % length(local.azs)]
  tags              = { Name = "${local.name}-private-${count.index}" }
}
resource "aws_eip" "nat" {
  count      = length(local.azs)
  domain     = "vpc"
  tags       = { Name = "${local.name}-nat-eip-${count.index}" }
  depends_on = [aws_internet_gateway.main]
}
resource "aws_nat_gateway" "main" {
  count         = length(local.azs)
  subnet_id     = aws_subnet.public[count.index].id
  allocation_id = aws_eip.nat[count.index].id
  tags          = { Name = "${local.name}-nat-${count.index}" }
  depends_on    = [aws_internet_gateway.main]
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public" }
}
resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
resource "aws_route_table" "private" {
  count  = length(local.azs)
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[count.index].id
  }
  tags = { Name = "${local.name}-private-${count.index}" }
}
resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index % length(local.azs)].id
}
resource "aws_security_group" "alb" {
  name   = "${local.name}-alb"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-alb" }
}
resource "aws_security_group" "ecs_tasks" {
  name   = "${local.name}-ecs-tasks"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = var.app_container_port
    to_port         = var.app_container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    from_port = 0
    to_port   = 65535
    protocol  = "tcp"
    self      = true
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-ecs-tasks" }
}
resource "aws_security_group" "rds" {
  name   = "${local.name}-rds"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_tasks.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-rds" }
}
resource "aws_security_group" "redis" {
  name   = "${local.name}-redis"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_tasks.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-redis" }
}
resource "aws_security_group" "msk" {
  name   = "${local.name}-msk"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 9096
    to_port         = 9096
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_tasks.id]
  }
  ingress {
    from_port = 0
    to_port   = 65535
    protocol  = "tcp"
    self      = true
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-msk" }
}
resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = local.name }
}
resource "aws_rds_cluster" "main" {
  cluster_identifier              = local.name
  engine                          = "aurora-postgresql"
  engine_version                  = "16.13"
  database_name                   = var.postgres_db_name
  master_username                 = var.postgres_user
  master_password                 = local.rds_master_password
  db_subnet_group_name            = aws_db_subnet_group.main.name
  vpc_security_group_ids          = [aws_security_group.rds.id]
  skip_final_snapshot             = var.environment == "dev" ? true : false
  final_snapshot_identifier       = var.environment != "dev" ? "${local.name}-final" : null
  storage_encrypted               = var.enable_kms_encryption
  kms_key_id                      = var.enable_kms_encryption ? aws_kms_key.main[0].arn : null
  backup_retention_period         = 1 # Free tier limited to 1 day for dev
  preferred_backup_window         = "02:00-03:00"
  preferred_maintenance_window    = "sun:03:00-sun:04:00"
  deletion_protection             = var.environment == "prod"
  copy_tags_to_snapshot           = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  tags                            = { Name = local.name }
}
resource "aws_rds_cluster_instance" "main" {
  count               = 2
  cluster_identifier  = aws_rds_cluster.main.id
  instance_class      = var.rds_instance_class
  engine              = aws_rds_cluster.main.engine
  engine_version      = aws_rds_cluster.main.engine_version
  publicly_accessible = false
  tags                = { Name = "${local.name}-${count.index}" }
}
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.private[*].id
}
resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${local.name}-redis"
  description                = "Redis for ${local.name}"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  port                       = 6379
  parameter_group_name       = "default.redis7"
  automatic_failover_enabled = true
  multi_az_enabled           = true
  num_cache_clusters         = var.redis_num_nodes
  security_group_ids         = [aws_security_group.redis.id]
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  at_rest_encryption_enabled = var.enable_kms_encryption
  kms_key_id                 = var.enable_kms_encryption ? aws_kms_key.main[0].arn : null
  transit_encryption_enabled = var.enable_kms_encryption
  auth_token                 = var.enable_kms_encryption ? local.redis_auth_token : ""
  snapshot_retention_limit   = 5
  snapshot_window            = "02:00-03:00"
  apply_immediately          = true
  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis_engine.name
    destination_type = "cloudwatch-logs"
    log_type         = "engine-log"
    log_format       = "text"
  }
  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis_slow.name
    destination_type = "cloudwatch-logs"
    log_type         = "slow-log"
    log_format       = "text"
  }
  tags                       = { Name = local.name }
}
resource "aws_msk_configuration" "main" {
  name              = "${local.name}-config"
  kafka_versions    = ["3.6.0"]
  server_properties = file("${path.module}/msk/server.properties")
}
resource "aws_msk_cluster" "main" {
  cluster_name           = local.name
  kafka_version          = "3.6.0"
  number_of_broker_nodes = 2 # Must be a multiple of private_subnet_cidrs (2 AZs)

  broker_node_group_info {
    instance_type   = "kafka.t3.small"
    client_subnets  = aws_subnet.private[*].id
    security_groups = [aws_security_group.msk.id]
    storage_info {
      ebs_storage_info {
        volume_size = 100
      }
    }
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = var.enable_kms_encryption ? aws_kms_key.main[0].arn : null
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  client_authentication {
    sasl {
      iam   = false
      scram = true
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.main.arn
    revision = aws_msk_configuration.main.latest_revision
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk.name
      }
    }
  }

  tags = { Name = local.name }
}
resource "aws_ecr_repository" "app" {
  name                 = "${local.name}-app"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }

  # Suppress tags to avoid ecr:TagResource permissions error
  tags = {
    Name        = null
    Project     = null
    Environment = null
    ManagedBy   = null
  }

  lifecycle {
    # Ignore tag changes to avoid permission issues in limited IAM environments
    ignore_changes = [tags_all]
    # The workflow may pre-create this repository for image pushes; never let Terraform delete it.
    prevent_destroy = true
  }
}
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 30 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = {
        type = "expire"
      }
    }]
  })
}
resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
  # Not setting kms_key_id here unless we configure the KMS key policy to allow logs service
  tags = { Name = local.name }
}
resource "aws_cloudwatch_log_group" "msk" {
  name              = "/msk/${local.name}"
  retention_in_days = var.log_retention_days
  tags              = { Name = local.name }
}
resource "aws_cloudwatch_log_group" "redis_engine" {
  name              = "/elasticache/${local.name}/engine"
  retention_in_days = var.log_retention_days
  tags              = { Name = local.name }
}
resource "aws_cloudwatch_log_group" "redis_slow" {
  name              = "/elasticache/${local.name}/slow"
  retention_in_days = var.log_retention_days
  tags              = { Name = local.name }
}
resource "aws_iam_role" "ecs_task_execution_role" {
  name = "${local.name}-ecs-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  # Suppress tags to avoid iam:TagRole permissions error
  tags = {
    Name        = null
    Project     = null
    Environment = null
    ManagedBy   = null
  }

  lifecycle {
    # Ignore tag changes to avoid permission issues in limited IAM environments
    ignore_changes = [tags_all]
  }
}
resource "aws_iam_role_policy_attachment" "ecs_task_execution_role_policy" {
  role       = aws_iam_role.ecs_task_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role" "ecs_task_role" {
  name = "${local.name}-ecs-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  # Suppress tags to avoid iam:TagRole permissions error
  tags = {
    Name        = null
    Project     = null
    Environment = null
    ManagedBy   = null
  }

  lifecycle {
    # Ignore tag changes to avoid permission issues in limited IAM environments
    ignore_changes = [tags_all]
  }
}
resource "aws_iam_role_policy" "ecs_task_policy" {
  name = "${local.name}-ecs-task-policy"
  role = aws_iam_role.ecs_task_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "glue:GetSchema",
        "glue:GetSchemaVersion",
        "glue:GetSchemaByDefinition"
      ]
      Resource = [
        "${aws_cloudwatch_log_group.ecs.arn}:*",
        "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:registry/${var.schema_registry_name}",
        "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:schema/${var.schema_registry_name}/${var.schema_name}"
      ]
    }]
  })
}
# Secrets Manager
resource "aws_secretsmanager_secret" "postgres_password" {
  name                    = "${local.name}/postgres/password"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "postgres_password" {
  secret_id     = aws_secretsmanager_secret.postgres_password.id
  secret_string = local.rds_master_password
}

resource "aws_secretsmanager_secret" "redis_password" {
  name                    = "${local.name}/redis/password"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "redis_password" {
  secret_id     = aws_secretsmanager_secret.redis_password.id
  secret_string = local.redis_auth_token
}

data "aws_secretsmanager_secret" "reddit_credentials" {
  name = "${local.name}/reddit/credentials"
}

resource "aws_secretsmanager_secret" "kafka_scram" {
  name                    = "AmazonMSK_${local.name}/kafka/scram"
  recovery_window_in_days = 7
  kms_key_id              = var.enable_kms_encryption ? aws_kms_key.main[0].arn : null
}

resource "aws_secretsmanager_secret_version" "kafka_scram" {
  secret_id = aws_secretsmanager_secret.kafka_scram.id
  secret_string = jsonencode({
    username = var.kafka_sasl_username
    password = local.kafka_sasl_password
  })
}

resource "aws_msk_scram_secret_association" "main" {
  cluster_arn     = aws_msk_cluster.main.arn
  secret_arn_list = [aws_secretsmanager_secret.kafka_scram.arn]
}

resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  name = "${local.name}-ecs-execution-secrets"
  role = aws_iam_role.ecs_task_execution_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ]
      Resource = [
        aws_secretsmanager_secret.postgres_password.arn,
        aws_secretsmanager_secret.redis_password.arn,
        data.aws_secretsmanager_secret.reddit_credentials.arn,
        aws_secretsmanager_secret.kafka_scram.arn
      ]
    }]
  })
}

resource "aws_iam_role_policy" "ecs_task_execution_kms" {
  count = var.enable_kms_encryption ? 1 : 0
  name  = "${local.name}-ecs-execution-kms"
  role  = aws_iam_role.ecs_task_execution_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "kms:Decrypt",
        "kms:DescribeKey"
      ]
      Resource = aws_kms_key.main[0].arn
    }]
  })
}

resource "aws_lb" "main" {
  name                       = local.name
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  enable_deletion_protection = var.environment == "prod" ? true : false
  tags                       = { Name = local.name }
}
resource "aws_lb_target_group" "app" {
  name        = "${local.name}-app"
  port        = var.app_container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"
  health_check {
    healthy_threshold   = 2
    unhealthy_threshold = 5
    timeout             = 10
    interval            = 20
    path                = "/health"
    matcher             = "200"
  }
}
resource "aws_lb_listener" "http" {
  count             = local.enable_https ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = "80"
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = local.enable_https ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = "443"
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS-1-2-2017-01"
  certificate_arn   = var.acm_certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_lb_listener" "http_plain" {
  count             = local.enable_https ? 0 : 1
  load_balancer_arn = aws_lb.main.arn
  port              = "80"
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
resource "aws_ecs_cluster" "main" {
  name = local.name
  tags = { Name = local.name }
}
resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
  default_capacity_provider_strategy {
    base              = 1
    weight            = 100
    capacity_provider = "FARGATE"
  }
}
resource "aws_ecs_task_definition" "app" {
  family                   = "${local.name}-app"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.app_cpu
  memory                   = var.app_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn
  container_definitions = jsonencode([{
    name      = "app"
    image     = "${aws_ecr_repository.app.repository_url}:${var.app_image_tag}"
    essential = true
    portMappings = [{
      containerPort = var.app_container_port
      protocol      = "tcp"
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "app"
      }
    }
    environment = [
      { name = "POSTGRES_HOST", value = aws_rds_cluster.main.endpoint },
      { name = "POSTGRES_PORT", value = "5432" },
      { name = "POSTGRES_DB", value = var.postgres_db_name },
      { name = "POSTGRES_USER", value = var.postgres_user },
      { name = "REDDIT_USER_AGENT", value = var.reddit_user_agent },
      { name = "REDIS_HOST", value = aws_elasticache_replication_group.redis.primary_endpoint_address },
      { name = "REDIS_PORT", value = "6379" },
      { name = "REDIS_USER", value = var.redis_user },
      { name = "REDIS_USE_SSL", value = tostring(var.enable_kms_encryption) },
      { name = "KAFKA_BOOTSTRAP_SERVERS", value = aws_msk_cluster.main.bootstrap_brokers_sasl_scram },
      { name = "KAFKA_SECURITY_PROTOCOL", value = var.kafka_security_protocol },
      { name = "KAFKA_SASL_MECHANISM", value = "SCRAM-SHA-512" },
      { name = "KAFKA_SSL_CA_LOCATION", value = "/etc/ssl/certs/ca-certificates.crt" },
      { name = "KAFKA_RAW_TEXT_TOPIC", value = var.kafka_raw_text_topic },

      { name = "SCHEMA_REGISTRY_NAME", value = var.schema_registry_name },
      { name = "SCHEMA_NAME", value = var.schema_name },
      { name = "SCHEMA_VERSION", value = tostring(var.schema_version) },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "USE_LOCALSTACK", value = tostring(var.use_localstack) },
      { name = "DB_FLUSH_INTERVAL", value = tostring(var.db_flush_interval) },
      { name = "DEAD_STREAM_CLEANUP_INTERVAL", value = tostring(var.dead_stream_cleanup_interval) },
      { name = "LOG_LEVEL", value = var.log_level }


    ]
    secrets = [
      { name = "POSTGRES_PASSWORD", valueFrom = aws_secretsmanager_secret.postgres_password.arn },
      { name = "REDIS_PASSWORD", valueFrom = aws_secretsmanager_secret.redis_password.arn },
      { name = "REDDIT_CLIENT_ID", valueFrom = "${data.aws_secretsmanager_secret.reddit_credentials.arn}:REDDIT_CLIENT_ID::" },
      { name = "REDDIT_CLIENT_SECRET", valueFrom = "${data.aws_secretsmanager_secret.reddit_credentials.arn}:REDDIT_CLIENT_SECRET::" },
      { name = "KAFKA_SASL_USERNAME", valueFrom = "${aws_secretsmanager_secret.kafka_scram.arn}:username::" },
      { name = "KAFKA_SASL_PASSWORD", valueFrom = "${aws_secretsmanager_secret.kafka_scram.arn}:password::" }
    ]
  }])
  tags = { Name = local.name }
}
resource "aws_ecs_service" "app" {
  name                               = "${local.name}-app"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.app.arn
  desired_count                      = var.app_desired_count
  enable_execute_command             = true
  health_check_grace_period_seconds  = 300
  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = var.app_container_port
  }
  depends_on = [
    aws_rds_cluster_instance.main,
  ]
  tags = { Name = local.name }
}
resource "aws_appautoscaling_target" "app" {
  max_capacity       = 10
  min_capacity       = var.app_desired_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}
resource "aws_appautoscaling_policy" "cpu" {
  name               = "${local.name}-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = 70.0
  }
}
resource "aws_appautoscaling_policy" "memory" {
  name               = "${local.name}-memory"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageMemoryUtilization"
    }
    target_value = 80.0
  }
}
resource "aws_cloudwatch_metric_alarm" "unhealthy_targets" {
  alarm_name          = "${local.name}-unhealthy"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Average"
  threshold           = var.app_desired_count
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }
}
resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${local.name}-rds-cpu"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  dimensions = {
    DBClusterIdentifier = aws_rds_cluster.main.cluster_identifier
  }
}

resource "aws_cloudwatch_metric_alarm" "ecs_cpu" {
  alarm_name          = "${local.name}-ecs-cpu"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  treat_missing_data  = "notBreaching"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.app.name
  }
}

resource "aws_cloudwatch_metric_alarm" "ecs_memory" {
  alarm_name          = "${local.name}-ecs-memory"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "MemoryUtilization"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  treat_missing_data  = "notBreaching"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.app.name
  }
}
