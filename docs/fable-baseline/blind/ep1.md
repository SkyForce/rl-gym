```hcl
###############################################################################
# Secure Amazon MSK cluster with KMS encryption at rest
#
# Security posture:
#   - Encryption at rest with a customer-managed KMS key (rotation enabled)
#   - TLS-only encryption in transit (client-broker and in-cluster)
#   - IAM (SASL) authentication only — no passwords/certs in code, and
#     unauthenticated access explicitly disabled
#   - MSK public access DISABLED; brokers never receive public IPs
#   - Least-privilege security group via standalone rule resources:
#     VPC-internal TLS ports only, no 0.0.0.0/0 ingress or egress
#   - Broker logs shipped to an encrypted CloudWatch log group (1y retention)
#   - Consistent tagging on all taggable resources
###############################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.30"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

###############################################################################
# Variables (no secrets — authentication is IAM-based)
###############################################################################

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project identifier used for naming and tagging"
  type        = string
  default     = "secure-msk"
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
  default     = "prod"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "kafka_version" {
  description = "Apache Kafka version for the MSK cluster"
  type        = string
  default     = "3.6.0"
}

variable "broker_instance_type" {
  description = "Instance type for MSK broker nodes"
  type        = string
  default     = "kafka.m5.large"
}

variable "broker_ebs_volume_size" {
  description = "EBS storage volume size (GiB) per broker"
  type        = number
  default     = 100
}

locals {
  cluster_name = "${var.project}-${var.environment}"

  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Service     = "msk"
  }
}

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

###############################################################################
# Networking
###############################################################################

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-vpc"
  })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-igw"
  })
}

# Broker subnets across three AZs. Instances launched here never receive
# public IPs, and MSK broker ENIs are private-only by design.
resource "aws_subnet" "msk" {
  count = 3

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-subnet-${count.index + 1}"
  })
}

resource "aws_route_table" "msk" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-rt"
  })
}

resource "aws_route_table_association" "msk" {
  count = length(aws_subnet.msk)

  subnet_id      = aws_subnet.msk[count.index].id
  route_table_id = aws_route_table.msk.id
}

###############################################################################
# Least-privilege security group (rules managed as standalone resources)
###############################################################################

resource "aws_security_group" "msk" {
  name        = "${local.cluster_name}-broker-sg"
  description = "Least-privilege access to MSK brokers: TLS ports from inside the VPC only"
  vpc_id      = aws_vpc.main.id

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-broker-sg"
  })
}

# Intra-cluster traffic (broker-to-broker / ZooKeeper), restricted to members
# of this security group only.
resource "aws_vpc_security_group_ingress_rule" "intra_cluster" {
  security_group_id            = aws_security_group.msk.id
  referenced_security_group_id = aws_security_group.msk.id
  ip_protocol                  = "-1"
  description                  = "Self-referenced intra-cluster broker communication"

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-intra-cluster"
  })
}

resource "aws_vpc_security_group_ingress_rule" "clients_tls" {
  security_group_id = aws_security_group.msk.id
  cidr_ipv4         = aws_vpc.main.cidr_block
  from_port         = 9094
  to_port           = 9094
  ip_protocol       = "tcp"
  description       = "Kafka TLS client access from within the VPC only"

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-clients-tls"
  })
}

resource "aws_vpc_security_group_ingress_rule" "clients_sasl_iam" {
  security_group_id = aws_security_group.msk.id
  cidr_ipv4         = aws_vpc.main.cidr_block
  from_port         = 9098
  to_port           = 9098
  ip_protocol       = "tcp"
  description       = "Kafka SASL/IAM (over TLS) client access from within the VPC only"

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-clients-sasl-iam"
  })
}

# No internet egress: brokers may only reach VPC-internal destinations.
resource "aws_vpc_security_group_egress_rule" "vpc_only" {
  security_group_id = aws_security_group.msk.id
  cidr_ipv4         = aws_vpc.main.cidr_block
  ip_protocol       = "-1"
  description       = "Restrict broker egress to VPC-internal traffic only"

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-egress-vpc-only"
  })
}

###############################################################################
# KMS key for encryption at rest (and encrypted broker logs)
###############################################################################

resource "aws_kms_key" "msk" {
  description             = "CMK for MSK encryption at rest and encrypted broker logs"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowKeyAdministrationViaIAM"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogsEncryption"
        Effect = "Allow"
        Principal = {
          Service = "logs.${data.aws_region.current.name}.amazonaws.com"
        }
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
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:*"
          }
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-kms"
  })
}

# Encrypted broker log destination with one-year retention.
resource "aws_cloudwatch_log_group" "msk_broker" {
  name              = "/aws/msk/${local.cluster_name}/brokers"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.msk.arn

  tags = merge(local.common_tags, {
    Name = "${local.cluster_name}-broker-logs"
  })
}

###############################################################################
# MSK cluster
###############################################################################

resource "aws_msk_cluster" "main" {
  cluster_name           = local.cluster_name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = length(aws_subnet.msk)
  enhanced_monitoring    = "PER_TOPIC_PER_BROKER"

  broker_node_group_info {
    instance_type   = var.broker_instance_type
    client_subnets  = aws_subnet.msk[*].id
    security_groups = [aws_security_group.msk.id]

    storage_info {
      ebs_storage_info {
        volume_size = var.broker_ebs_volume_size
      }
    }

    connectivity_info {
      public_access {
        type = "DISABLED" # brokers are reachable only from inside the VPC
      }
    }
  }

  encryption_info {
    # Encryption at rest with the customer-managed KMS key
    encryption_at_rest_kms_key_arn = aws_kms_key.msk.arn

    # TLS-only encryption in transit
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  client_authentication {
    unauthenticated = false

    sasl {
      iam = true # IAM authentication — no static credentials in code
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

  tags = merge(local.common_tags, {
    Name = local.cluster_name
  })
}

###############################################################################
# Outputs
###############################################################################

output "msk_cluster_arn" {
  description = "ARN of the MSK cluster"
  value       = aws_msk_cluster.main.arn
}

output "bootstrap_brokers_sasl_iam" {
  description = "TLS bootstrap broker endpoints for SASL/IAM clients"
  value       = aws_msk_cluster.main.bootstrap_brokers_sasl_iam
}

output "kms_key_arn" {
  description = "ARN of the KMS key used for encryption at rest"
  value       = aws_kms_key.msk.arn
}

output "vpc_id" {
  description = "ID of the VPC hosting the MSK cluster"
  value       = aws_vpc.main.id
}
```