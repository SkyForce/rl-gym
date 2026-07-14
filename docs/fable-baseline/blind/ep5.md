```hcl
###############################################################################
# Secure MSK + MSK Connect (Debezium) stack — us-east-1
# - Brokers in private subnets, TLS-only, IAM auth, CMK encryption at rest
# - Least-privilege IAM, no public access, no hardcoded secrets, tagged
###############################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

###############################################################################
# Inputs — secrets are injected (TF_VAR_* / tfvars / vault), never hardcoded
###############################################################################

variable "database_hostname" {
  description = "Hostname of the source database Debezium captures changes from"
  type        = string
  default     = "mysql.internal.example.com"
}

variable "database_name" {
  description = "Database (schema) to include in change data capture"
  type        = string
  default     = "inventory"
}

variable "database_username" {
  description = "Source database user for Debezium (supply externally, never commit)"
  type        = string
  sensitive   = true
}

variable "database_password" {
  description = "Source database password for Debezium (supply externally, never commit)"
  type        = string
  sensitive   = true
}

locals {
  cluster_name = "secure-msk-cluster"

  common_tags = {
    Project     = "msk-debezium-cdc"
    Environment = "production"
    ManagedBy   = "terraform"
    Owner       = "data-platform"
  }

  # Derive least-privilege topic/group ARN patterns from the cluster ARN
  msk_topic_arns = "${replace(aws_msk_cluster.main.arn, ":cluster/", ":topic/")}/*"
  msk_group_arns = "${replace(aws_msk_cluster.main.arn, ":cluster/", ":group/")}/*"
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

###############################################################################
# KMS CMK for encryption at rest (MSK, S3, CloudWatch Logs), rotation enabled
###############################################################################

resource "aws_kms_key" "main" {
  description             = "CMK for MSK at-rest encryption, plugin bucket, and log groups"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableRootAccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowCloudWatchLogsUseOfKey"
        Effect    = "Allow"
        Principal = { Service = "logs.us-east-1.amazonaws.com" }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:us-east-1:${data.aws_caller_identity.current.account_id}:log-group:*"
          }
        }
      }
    ]
  })

  tags = local.common_tags
}

###############################################################################
# Networking — MSK lives in private subnets; IGW serves a separate public tier
###############################################################################

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, { Name = "${local.cluster_name}-vpc" })
}

# Lock down the default security group (no ingress/egress rules)
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.common_tags, { Name = "${local.cluster_name}-default-sg-locked" })
}

# Private subnets for MSK brokers and MSK Connect workers (no public IPs)
resource "aws_subnet" "private" {
  count                   = 3
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-private-${data.aws_availability_zones.available.names[count.index]}"
    Tier = "private"
  })
}

# Public subnet (e.g. for a future NAT gateway) — MSK is never placed here
resource "aws_subnet" "public" {
  count                   = 1
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, 100)
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-public-${data.aws_availability_zones.available.names[0]}"
    Tier = "public"
  })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.common_tags, { Name = "${local.cluster_name}-igw" })
}

# Private route table: local VPC routing only — no internet path for brokers
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.common_tags, { Name = "${local.cluster_name}-private-rt" })
}

resource "aws_route_table_association" "private" {
  count          = 3
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Public route table: default route to the IGW, only for the public tier
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(local.common_tags, { Name = "${local.cluster_name}-public-rt" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public[0].id
  route_table_id = aws_route_table.public.id
}

###############################################################################
# VPC flow logs (encrypted, 1-year retention)
###############################################################################

resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/vpc/${local.cluster_name}/flow-logs"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.main.arn
  tags              = local.common_tags
}

resource "aws_iam_role" "flow_logs" {
  name        = "${local.cluster_name}-flow-logs-role"
  description = "Allows VPC Flow Logs to deliver to CloudWatch Logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_policy" "flow_logs" {
  name        = "${local.cluster_name}-flow-logs-policy"
  description = "Least-privilege delivery permissions for VPC flow logs"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "DeliverFlowLogs"
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams"
      ]
      Resource = "${aws_cloudwatch_log_group.flow_logs.arn}:*"
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "flow_logs" {
  role       = aws_iam_role.flow_logs.name
  policy_arn = aws_iam_policy.flow_logs.arn
}

resource "aws_flow_log" "main" {
  vpc_id               = aws_vpc.main.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow_logs.arn
  iam_role_arn         = aws_iam_role.flow_logs.arn
  tags                 = local.common_tags
}

###############################################################################
# Security group — self-referenced only, no 0.0.0.0/0 anywhere
###############################################################################

resource "aws_security_group" "msk" {
  name        = "${local.cluster_name}-sg"
  description = "MSK brokers and MSK Connect workers; intra-SG Kafka traffic only"
  vpc_id      = aws_vpc.main.id

  tags = merge(local.common_tags, { Name = "${local.cluster_name}-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "kafka_tls" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Kafka TLS (9094) from members of this security group only"
  referenced_security_group_id = aws_security_group.msk.id
  from_port                    = 9094
  to_port                      = 9094
  ip_protocol                  = "tcp"
  tags                         = local.common_tags
}

resource "aws_vpc_security_group_ingress_rule" "kafka_iam" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Kafka SASL/IAM (9098) from members of this security group only"
  referenced_security_group_id = aws_security_group.msk.id
  from_port                    = 9098
  to_port                      = 9098
  ip_protocol                  = "tcp"
  tags                         = local.common_tags
}

resource "aws_vpc_security_group_egress_rule" "vpc_only" {
  security_group_id = aws_security_group.msk.id
  description       = "Restrict egress to the VPC CIDR (extend deliberately for the source DB)"
  cidr_ipv4         = aws_vpc.main.cidr_block
  ip_protocol       = "-1"
  tags              = local.common_tags
}

###############################################################################
# Log groups for MSK broker logs and MSK Connect worker logs
###############################################################################

resource "aws_cloudwatch_log_group" "msk_broker" {
  name              = "/msk/${local.cluster_name}/broker-logs"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.main.arn
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "msk_connect" {
  name              = "/msk/${local.cluster_name}/connect-worker-logs"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.main.arn
  tags              = local.common_tags
}

###############################################################################
# MSK cluster — 3 brokers, CMK at rest, TLS in transit, IAM auth, no public access
###############################################################################

resource "aws_msk_cluster" "main" {
  cluster_name           = local.cluster_name
  kafka_version          = "3.5.1"
  number_of_broker_nodes = 3
  enhanced_monitoring    = "PER_TOPIC_PER_BROKER"

  broker_node_group_info {
    instance_type   = "kafka.m5.large"
    client_subnets  = aws_subnet.private[*].id
    security_groups = [aws_security_group.msk.id]

    storage_info {
      ebs_storage_info {
        volume_size = 100
      }
    }

    connectivity_info {
      public_access {
        type = "DISABLED"
      }
    }
  }

  client_authentication {
    sasl {
      iam = true
    }
    unauthenticated = false
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = aws_kms_key.main.arn

    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk_broker.name
      }
    }
  }

  tags = local.common_tags
}

###############################################################################
# S3 bucket for the Debezium plugin — private, versioned, KMS-encrypted, TLS-only
###############################################################################

resource "aws_s3_bucket" "plugin" {
  bucket_prefix = "msk-connect-plugins-"
  tags          = merge(local.common_tags, { Name = "${local.cluster_name}-connect-plugins" })
}

resource "aws_s3_bucket_public_access_block" "plugin" {
  bucket                  = aws_s3_bucket.plugin.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "plugin" {
  bucket = aws_s3_bucket.plugin.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "plugin" {
  bucket = aws_s3_bucket.plugin.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_policy" "plugin" {
  bucket     = aws_s3_bucket.plugin.id
  depends_on = [aws_s3_bucket_public_access_block.plugin]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.plugin.arn,
        "${aws_s3_bucket.plugin.arn}/*"
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}

resource "aws_s3_object" "debezium_plugin" {
  bucket                 = aws_s3_bucket.plugin.id
  key                    = "plugins/debezium.zip"
  source                 = "${path.module}/debezium.zip"
  content_type           = "application/zip"
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.main.arn

  tags = local.common_tags

  depends_on = [
    aws_s3_bucket_versioning.plugin,
    aws_s3_bucket_server_side_encryption_configuration.plugin
  ]
}

###############################################################################
# MSK Connect custom plugin from debezium.zip
###############################################################################

resource "aws_mskconnect_custom_plugin" "debezium" {
  name         = "${local.cluster_name}-debezium-plugin"
  description  = "Debezium MySQL source connector plugin"
  content_type = "ZIP"

  location {
    s3 {
      bucket_arn     = aws_s3_bucket.plugin.arn
      file_key       = aws_s3_object.debezium_plugin.key
      object_version = aws_s3_object.debezium_plugin.version_id
    }
  }

  tags = local.common_tags
}

###############################################################################
# Least-privilege service execution role for the connector (IAM auth to MSK)
###############################################################################

resource "aws_iam_role" "connector" {
  name        = "${local.cluster_name}-connector-role"
  description = "Service execution role assumed by MSK Connect for the Debezium connector"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "kafkaconnect.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_policy" "connector" {
  name        = "${local.cluster_name}-connector-policy"
  description = "Scoped kafka-cluster permissions for the Debezium MSK Connect connector"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ClusterAccess"
        Effect = "Allow"
        Action = [
          "kafka-cluster:Connect",
          "kafka-cluster:DescribeCluster",
          "kafka-cluster:WriteDataIdempotently"
        ]
        Resource = aws_msk_cluster.main.arn
      },
      {
        Sid    = "TopicAccess"
        Effect = "Allow"
        Action = [
          "kafka-cluster:CreateTopic",
          "kafka-cluster:DescribeTopic",
          "kafka-cluster:ReadData",
          "kafka-cluster:WriteData"
        ]
        Resource = local.msk_topic_arns
      },
      {
        Sid    = "ConsumerGroupAccess"
        Effect = "Allow"
        Action = [
          "kafka-cluster:AlterGroup",
          "kafka-cluster:DescribeGroup"
        ]
        Resource = local.msk_group_arns
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "connector" {
  role       = aws_iam_role.connector.name
  policy_arn = aws_iam_policy.connector.arn
}

###############################################################################
# MSK Connect connector — custom plugin + IAM-authenticated TLS to the cluster
###############################################################################

resource "aws_mskconnect_connector" "debezium" {
  name                 = "${local.cluster_name}-debezium-connector"
  description          = "Debezium MySQL CDC connector streaming into MSK"
  kafkaconnect_version = "2.7.1"

  capacity {
    autoscaling {
      mcu_count        = 1
      min_worker_count = 1
      max_worker_count = 2

      scale_in_policy {
        cpu_utilization_percentage = 20
      }

      scale_out_policy {
        cpu_utilization_percentage = 80
      }
    }
  }

  connector_configuration = {
    "connector.class"                                            = "io.debezium.connector.mysql.MySqlConnector"
    "tasks.max"                                                  = "1"
    "database.hostname"                                          = var.database_hostname
    "database.port"                                              = "3306"
    "database.user"                                              = var.database_username
    "database.password"                                          = var.database_password
    "database.server.id"                                         = "184054"
    "database.include.list"                                      = var.database_name
    "topic.prefix"                                               = "cdc"
    "include.schema.changes"                                     = "true"
    "schema.history.internal.kafka.topic"                        = "schema-changes.${var.database_name}"
    "schema.history.internal.kafka.bootstrap.servers"            = aws_msk_cluster.main.bootstrap_brokers_sasl_iam
    "schema.history.internal.producer.security.protocol"         = "SASL_SSL"
    "schema.history.internal.producer.sasl.mechanism"            = "AWS_MSK_IAM"
    "schema.history.internal.producer.sasl.jaas.config"          = "software.amazon.msk.auth.iam.IAMLoginModule required;"
    "schema.history.internal.producer.sasl.client.callback.handler.class" = "software.amazon.msk.auth.iam.IAMClientCallbackHandler"
    "schema.history.internal.consumer.security.protocol"         = "SASL_SSL"
    "schema.history.internal.consumer.sasl.mechanism"            = "AWS_MSK_IAM"
    "schema.history.internal.consumer.sasl.jaas.config"          = "software.amazon.msk.auth.iam.IAMLoginModule required;"
    "schema.history.internal.consumer.sasl.client.callback.handler.class" = "software.amazon.msk.auth.iam.IAMClientCallbackHandler"
  }

  kafka_cluster {
    apache_kafka_cluster {
      bootstrap_servers = aws_msk_cluster.main.bootstrap_brokers_sasl_iam

      vpc {
        security_groups = [aws_security_group.msk.id]
        subnets         = aws_subnet.private[*].id
      }
    }
  }

  kafka_cluster_client_authentication {
    authentication_type = "IAM"
  }

  kafka_cluster_encryption_in_transit {
    encryption_type = "TLS"
  }

  plugin {
    custom_plugin {
      arn      = aws_mskconnect_custom_plugin.debezium.arn
      revision = aws_mskconnect_custom_plugin.debezium.latest_revision
    }
  }

  log_delivery {
    worker_log_delivery {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk_connect.name
      }
    }
  }

  service_execution_role_arn = aws_iam_role.connector.arn

  tags = local.common_tags

  depends_on = [aws_iam_role_policy_attachment.connector]
}
```