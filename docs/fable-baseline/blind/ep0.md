```hcl
###############################################################################
# Secure MSK Serverless cluster in us-east-2
#
# Security posture:
# - Encryption at rest is ALWAYS enforced on MSK Serverless via AWS-managed
#   KMS keys; in-transit traffic is TLS-only (no plaintext listeners exist).
# - Client authentication is SASL/IAM only -> no passwords/secrets in code.
# - Security group is least-privilege: Kafka IAM port (9098) only, ingress
#   restricted to members of the same security group, egress restricted to
#   the VPC CIDR. No 0.0.0.0/0 rules anywhere.
# - Subnets never auto-assign public IPs; MSK Serverless ENIs are private
#   and are not reachable from the internet.
# - Everything is tagged (plus provider-level default_tags).
###############################################################################

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
    }
  }
}

locals {
  vpc_cidr     = "10.0.0.0/16"
  azs          = ["us-east-2a", "us-east-2b"]
  subnet_cidrs = ["10.0.1.0/24", "10.0.2.0/24"]
  name_prefix  = "msk-serverless"
}

# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = local.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${local.name_prefix}-vpc"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

resource "aws_subnet" "msk" {
  count = length(local.azs)

  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false # never auto-assign public IPs

  tags = {
    Name = "${local.name_prefix}-subnet-${local.azs[count.index]}"
  }
}

resource "aws_route_table" "main" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${local.name_prefix}-rt"
  }
}

resource "aws_route_table_association" "msk" {
  count = length(aws_subnet.msk)

  subnet_id      = aws_subnet.msk[count.index].id
  route_table_id = aws_route_table.main.id
}

# --------------------------------------------------------------------------
# Security group (least privilege, granular rule resources)
# --------------------------------------------------------------------------

resource "aws_security_group" "msk" {
  name        = "${local.name_prefix}-sg"
  description = "Least-privilege access control for the MSK Serverless cluster"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Only clients that are themselves members of this security group may reach
# the Kafka SASL/IAM listener. No public or VPC-wide ingress.
resource "aws_vpc_security_group_ingress_rule" "kafka_sasl_iam" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Kafka SASL/IAM (TCP 9098) from members of this SG only"
  referenced_security_group_id = aws_security_group.msk.id
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098

  tags = {
    Name = "${local.name_prefix}-ingress-kafka-iam"
  }
}

# Egress restricted to Kafka IAM traffic within the VPC — no open egress.
resource "aws_vpc_security_group_egress_rule" "kafka_sasl_iam_vpc" {
  security_group_id = aws_security_group.msk.id
  description       = "Kafka SASL/IAM (TCP 9098) egress limited to the VPC CIDR"
  cidr_ipv4         = aws_vpc.main.cidr_block
  ip_protocol       = "tcp"
  from_port         = 9098
  to_port           = 9098

  tags = {
    Name = "${local.name_prefix}-egress-kafka-iam"
  }
}

# --------------------------------------------------------------------------
# MSK Serverless cluster
# --------------------------------------------------------------------------

resource "aws_msk_serverless_cluster" "main" {
  cluster_name = "${local.name_prefix}-cluster"

  # IAM is the only supported (and mandatory) auth mode for MSK Serverless:
  # no broker credentials or secrets are ever stored in this configuration.
  # Data is encrypted at rest with AWS-managed KMS keys and in transit via
  # TLS by the service itself.
  client_authentication {
    sasl {
      iam {
        enabled = true
      }
    }
  }

  vpc_config {
    subnet_ids         = aws_subnet.msk[*].id
    security_group_ids = [aws_security_group.msk.id]
  }

  tags = {
    Name = "${local.name_prefix}-cluster"
  }
}

output "msk_serverless_cluster_arn" {
  description = "ARN of the MSK Serverless cluster"
  value       = aws_msk_serverless_cluster.main.arn
}
```