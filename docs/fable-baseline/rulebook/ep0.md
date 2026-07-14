```hcl
# ---------------------------------------------------------------------------
# Serverless MSK cluster in us-east-2 (Ohio)
#
# Security posture:
#   - MSK Serverless encrypts data at rest by default with AWS-owned KMS keys
#     and enforces TLS for all client connections (in-transit encryption).
#   - Client access is IAM-authenticated (SASL/IAM) only - no plaintext,
#     no static credentials, no secrets in this configuration.
#   - Security group permits Kafka traffic (port 9098) only between members
#     of the same security group - no world-open ingress, no admin ports.
#   - Broker ENIs live in VPC subnets with no public IP auto-assignment.
#   - VPC Flow Logs enabled, delivered to CloudWatch Logs with retention,
#     via an IAM role scoped to the specific log group (least privilege).
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-2"

  default_tags {
    tags = {
      Project     = "msk-serverless"
      Environment = "production"
      ManagedBy   = "terraform"
      Owner       = "platform-engineering"
    }
  }
}

locals {
  name_prefix = "msk-serverless"
  vpc_cidr    = "10.0.0.0/16"
  azs         = ["us-east-2a", "us-east-2b"]
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = local.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${local.name_prefix}-vpc"
  }
}

resource "aws_subnet" "msk" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(local.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name_prefix}-subnet-${local.azs[count.index]}"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

resource "aws_route_table" "main" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${local.name_prefix}-rtb"
  }
}

resource "aws_route_table_association" "msk" {
  count = 2

  subnet_id      = aws_subnet.msk[count.index].id
  route_table_id = aws_route_table.main.id
}

# ---------------------------------------------------------------------------
# VPC Flow Logs (CloudWatch destination, scoped IAM role)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  name              = "/aws/vpc/${local.name_prefix}/flow-logs"
  retention_in_days = 365

  tags = {
    Name = "${local.name_prefix}-vpc-flow-logs"
  }
}

resource "aws_iam_role" "vpc_flow_logs" {
  name = "${local.name_prefix}-vpc-flow-logs-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowVpcFlowLogsService"
        Effect = "Allow"
        Action = "sts:AssumeRole"
        Principal = {
          Service = "vpc-flow-logs.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${local.name_prefix}-vpc-flow-logs-role"
  }
}

resource "aws_iam_role_policy" "vpc_flow_logs" {
  name = "${local.name_prefix}-vpc-flow-logs-policy"
  role = aws_iam_role.vpc_flow_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowFlowLogDelivery"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          aws_cloudwatch_log_group.vpc_flow_logs.arn
        ]
      }
    ]
  })
}

resource "aws_flow_log" "main" {
  vpc_id                   = aws_vpc.main.id
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.vpc_flow_logs.arn
  iam_role_arn             = aws_iam_role.vpc_flow_logs.arn
  max_aggregation_interval = 60

  tags = {
    Name = "${local.name_prefix}-vpc-flow-log"
  }
}

# ---------------------------------------------------------------------------
# Security group: intra-group Kafka SASL/IAM traffic only (least privilege)
# ---------------------------------------------------------------------------

resource "aws_security_group" "msk" {
  name        = "${local.name_prefix}-sg"
  description = "MSK Serverless - Kafka SASL IAM traffic restricted to this security group"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "msk_sasl_iam" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Kafka SASL IAM (9098) from clients in the same security group"
  referenced_security_group_id = aws_security_group.msk.id
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098

  tags = {
    Name = "${local.name_prefix}-ingress-sasl-iam"
  }
}

resource "aws_vpc_security_group_egress_rule" "msk_sasl_iam" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Kafka SASL IAM (9098) to members of the same security group"
  referenced_security_group_id = aws_security_group.msk.id
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098

  tags = {
    Name = "${local.name_prefix}-egress-sasl-iam"
  }
}

# ---------------------------------------------------------------------------
# MSK Serverless cluster (encryption at rest is always on; IAM auth only)
# ---------------------------------------------------------------------------

resource "aws_msk_serverless_cluster" "main" {
  cluster_name = "${local.name_prefix}-cluster"

  vpc_config {
    subnet_ids         = [aws_subnet.msk[0].id, aws_subnet.msk[1].id]
    security_group_ids = [aws_security_group.msk.id]
  }

  client_authentication {
    sasl {
      iam {
        enabled = true
      }
    }
  }

  tags = {
    Name = "${local.name_prefix}-cluster"
  }
}

output "msk_serverless_cluster_arn" {
  description = "ARN of the serverless MSK cluster"
  value       = aws_msk_serverless_cluster.main.arn
}
```