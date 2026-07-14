```hcl
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.31.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "msk-debezium-cdc"
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# Input variables (no hardcoded credentials anywhere in this configuration)
# ---------------------------------------------------------------------------

variable "database_hostname" {
  type        = string
  description = "Hostname of the source database Debezium captures changes from"
}

variable "database_username" {
  type        = string
  description = "Login user for the source database"
}

variable "database_password" {
  type        = string
  description = "Login credential for the source database (inject via TF_VAR or a secrets manager)"
  sensitive   = true
}

# ---------------------------------------------------------------------------
# KMS key for encryption at rest (AWS default key policy; rotation enabled)
# ---------------------------------------------------------------------------

resource "aws_kms_key" "main" {
  description             = "CMK for MSK broker storage and plugin artifact bucket encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = {
    Name = "msk-debezium-cmk"
  }
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "msk-debezium-vpc"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "msk-debezium-igw"
  }
}

resource "aws_subnet" "private_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "us-east-1a"
  map_public_ip_on_launch = false

  tags = {
    Name = "msk-private-us-east-1a"
  }
}

resource "aws_subnet" "private_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.2.0/24"
  availability_zone       = "us-east-1b"
  map_public_ip_on_launch = false

  tags = {
    Name = "msk-private-us-east-1b"
  }
}

resource "aws_subnet" "private_c" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.3.0/24"
  availability_zone       = "us-east-1c"
  map_public_ip_on_launch = false

  tags = {
    Name = "msk-private-us-east-1c"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.100.0/24"
  availability_zone       = "us-east-1a"
  map_public_ip_on_launch = false

  tags = {
    Name = "msk-public-us-east-1a"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "msk-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_flow_log" "vpc" {
  vpc_id               = aws_vpc.main.id
  traffic_type         = "ALL"
  log_destination_type = "s3"
  log_destination      = aws_s3_bucket.artifacts.arn

  tags = {
    Name = "msk-debezium-vpc-flow-logs"
  }
}

# ---------------------------------------------------------------------------
# Security group: no world-open ingress; broker traffic restricted to the SG
# ---------------------------------------------------------------------------

resource "aws_security_group" "msk" {
  name        = "msk-debezium-sg"
  description = "Security group for MSK brokers and MSK Connect workers"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "msk-debezium-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "kafka_self" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Kafka broker ports from members of this security group only"
  referenced_security_group_id = aws_security_group.msk.id
  from_port                    = 9092
  to_port                      = 9098
  ip_protocol                  = "tcp"

  tags = {
    Name = "kafka-intra-sg-ingress"
  }
}

resource "aws_vpc_security_group_egress_rule" "kafka_self" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Egress to Kafka brokers within this security group"
  referenced_security_group_id = aws_security_group.msk.id
  from_port                    = 9092
  to_port                      = 9098
  ip_protocol                  = "tcp"

  tags = {
    Name = "kafka-intra-sg-egress"
  }
}

resource "aws_vpc_security_group_egress_rule" "https_out" {
  security_group_id = aws_security_group.msk.id
  description       = "HTTPS egress for AWS service endpoints"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"

  tags = {
    Name = "https-egress"
  }
}

# ---------------------------------------------------------------------------
# CloudWatch log groups with retention
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "msk_broker" {
  name              = "/msk/broker/debezium-cluster"
  retention_in_days = 90

  tags = {
    Name = "msk-broker-logs"
  }
}

resource "aws_cloudwatch_log_group" "connect_worker" {
  name              = "/msk/connect/debezium-connector"
  retention_in_days = 90

  tags = {
    Name = "msk-connect-worker-logs"
  }
}

# ---------------------------------------------------------------------------
# S3 bucket for the Debezium plugin artifact (private, encrypted, versioned)
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "artifacts" {
  bucket = "msk-connect-debezium-artifacts-prod-use1"

  tags = {
    Name = "msk-connect-debezium-artifacts"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_object" "plugin" {
  bucket                 = aws_s3_bucket.artifacts.id
  key                    = "plugins/debezium.zip"
  source                 = "debezium.zip"
  content_type           = "application/zip"
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.main.arn

  tags = {
    Name = "debezium-plugin-archive"
  }
}

# ---------------------------------------------------------------------------
# IAM: least-privilege service execution role for MSK Connect
# ---------------------------------------------------------------------------

resource "aws_iam_role" "connect" {
  name        = "msk-connect-debezium-execution-role"
  description = "Service execution role assumed by MSK Connect for the Debezium connector"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "MSKConnectAssume"
        Effect = "Allow"
        Principal = {
          Service = "kafkaconnect.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "msk-connect-debezium-execution-role"
  }
}

resource "aws_iam_policy" "connect" {
  name        = "msk-connect-debezium-policy"
  description = "Least-privilege access to the MSK cluster and the plugin artifact"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ClusterAccess"
        Effect = "Allow"
        Action = [
          "kafka-cluster:Connect",
          "kafka-cluster:DescribeCluster",
          "kafka-cluster:DescribeClusterDynamicConfiguration"
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
        Resource = "${replace(aws_msk_cluster.main.arn, ":cluster/", ":topic/")}/*"
      },
      {
        Sid    = "GroupAccess"
        Effect = "Allow"
        Action = [
          "kafka-cluster:AlterGroup",
          "kafka-cluster:DescribeGroup"
        ]
        Resource = "${replace(aws_msk_cluster.main.arn, ":cluster/", ":group/")}/*"
      },
      {
        Sid    = "PluginObjectRead"
        Effect = "Allow"
        Action = [
          "s3:GetObject"
        ]
        Resource = "${aws_s3_bucket.artifacts.arn}/plugins/debezium.zip"
      },
      {
        Sid    = "PluginBucketRead"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = aws_s3_bucket.artifacts.arn
      }
    ]
  })

  tags = {
    Name = "msk-connect-debezium-policy"
  }
}

resource "aws_iam_role_policy_attachment" "connect" {
  role       = aws_iam_role.connect.name
  policy_arn = aws_iam_policy.connect.arn
}

# ---------------------------------------------------------------------------
# MSK cluster: 3 brokers, private subnets, TLS in transit, CMK at rest,
# IAM client auth only, no public access
# ---------------------------------------------------------------------------

resource "aws_msk_cluster" "main" {
  cluster_name           = "debezium-msk-cluster"
  kafka_version          = "3.6.0"
  number_of_broker_nodes = 3
  enhanced_monitoring    = "PER_TOPIC_PER_BROKER"

  broker_node_group_info {
    instance_type = "kafka.m5.large"
    client_subnets = [
      aws_subnet.private_a.id,
      aws_subnet.private_b.id,
      aws_subnet.private_c.id
    ]
    security_groups = [aws_security_group.msk.id]

    connectivity_info {
      public_access {
        type = "DISABLED"
      }
    }
  }

  client_authentication {
    unauthenticated = false

    sasl {
      iam = true
    }
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

  tags = {
    Name = "debezium-msk-cluster"
  }
}

# ---------------------------------------------------------------------------
# MSK Connect custom plugin from debezium.zip
# ---------------------------------------------------------------------------

resource "aws_mskconnect_custom_plugin" "debezium" {
  name         = "debezium-custom-plugin"
  description  = "Debezium source connector plugin archive"
  content_type = "ZIP"

  location {
    s3 {
      bucket_arn = aws_s3_bucket.artifacts.arn
      file_key   = aws_s3_object.plugin.key
    }
  }

  tags = {
    Name = "debezium-custom-plugin"
  }
}

# ---------------------------------------------------------------------------
# MSK Connect connector using the custom plugin against the MSK cluster
# ---------------------------------------------------------------------------

resource "aws_mskconnect_connector" "debezium" {
  name                 = "debezium-cdc-connector"
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
    "connector.class"                                                  = "io.debezium.connector.mysql.MySqlConnector"
    "tasks.max"                                                        = "1"
    "database.hostname"                                                = var.database_hostname
    "database.port"                                                    = "3306"
    "database.user"                                                    = var.database_username
    "database.password"                                                = var.database_password
    "database.server.id"                                               = "184054"
    "database.include.list"                                            = "inventory"
    "topic.prefix"                                                     = "debezium"
    "include.schema.changes"                                           = "true"
    "schema.history.internal.kafka.topic"                              = "debezium-schema-history"
    "schema.history.internal.kafka.bootstrap.servers"                  = aws_msk_cluster.main.bootstrap_brokers_sasl_iam
    "schema.history.internal.producer.security.protocol"               = "SASL_SSL"
    "schema.history.internal.producer.sasl.mechanism"                  = "AWS_MSK_IAM"
    "schema.history.internal.producer.sasl.jaas.config"                = "software.amazon.msk.auth.iam.IAMLoginModule required;"
    "schema.history.internal.producer.sasl.client.callback.handler.class" = "software.amazon.msk.auth.iam.IAMClientCallbackHandler"
    "schema.history.internal.consumer.security.protocol"               = "SASL_SSL"
    "schema.history.internal.consumer.sasl.mechanism"                  = "AWS_MSK_IAM"
    "schema.history.internal.consumer.sasl.jaas.config"                = "software.amazon.msk.auth.iam.IAMLoginModule required;"
    "schema.history.internal.consumer.sasl.client.callback.handler.class" = "software.amazon.msk.auth.iam.IAMClientCallbackHandler"
  }

  kafka_cluster {
    apache_kafka_cluster {
      bootstrap_servers = aws_msk_cluster.main.bootstrap_brokers_sasl_iam

      vpc {
        security_groups = [aws_security_group.msk.id]
        subnets = [
          aws_subnet.private_a.id,
          aws_subnet.private_b.id,
          aws_subnet.private_c.id
        ]
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
        log_group = aws_cloudwatch_log_group.connect_worker.name
      }
    }
  }

  service_execution_role_arn = aws_iam_role.connect.arn

  tags = {
    Name = "debezium-cdc-connector"
  }
}
```