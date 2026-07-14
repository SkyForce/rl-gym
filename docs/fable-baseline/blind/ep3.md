```hcl
###############################################################################
# Secure PostgreSQL on AWS RDS — custom VPC, private subnets, encrypted storage
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
  region = var.aws_region
}

###############################################################################
# Variables (no secrets hardcoded — master password is managed by RDS/Secrets Manager)
###############################################################################

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the custom VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "db_username" {
  description = "Master username for the PostgreSQL instance (password is auto-managed)"
  type        = string
  default     = "dbadmin"
}

locals {
  common_tags = {
    Project     = "secure-postgres"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

###############################################################################
# Networking — custom VPC with private subnets only (no public exposure)
###############################################################################

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, {
    Name = "secure-postgres-vpc"
  })
}

resource "aws_subnet" "db_private_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 0)
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false

  tags = merge(local.common_tags, {
    Name = "secure-postgres-private-a"
    Tier = "database"
  })
}

resource "aws_subnet" "db_private_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 1)
  availability_zone       = data.aws_availability_zones.available.names[1]
  map_public_ip_on_launch = false

  tags = merge(local.common_tags, {
    Name = "secure-postgres-private-b"
    Tier = "database"
  })
}

resource "aws_db_subnet_group" "postgres" {
  name        = "secure-postgres-subnet-group"
  description = "Private subnets for the PostgreSQL instance"
  subnet_ids  = [aws_subnet.db_private_a.id, aws_subnet.db_private_b.id]

  tags = merge(local.common_tags, {
    Name = "secure-postgres-subnet-group"
  })
}

###############################################################################
# Security group — least privilege: PostgreSQL port only, VPC-internal only
###############################################################################

resource "aws_security_group" "postgres" {
  name        = "secure-postgres-sg"
  description = "Least-privilege security group for the PostgreSQL RDS instance"
  vpc_id      = aws_vpc.main.id

  tags = merge(local.common_tags, {
    Name = "secure-postgres-sg"
  })
}

resource "aws_vpc_security_group_ingress_rule" "postgres_in" {
  security_group_id = aws_security_group.postgres.id
  description       = "Allow PostgreSQL (5432/tcp) only from inside the VPC"
  cidr_ipv4         = aws_vpc.main.cidr_block
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432

  tags = merge(local.common_tags, {
    Name = "secure-postgres-ingress-5432"
  })
}

resource "aws_vpc_security_group_egress_rule" "postgres_out" {
  security_group_id = aws_security_group.postgres.id
  description       = "Restrict egress to VPC-internal traffic only (no open internet egress)"
  cidr_ipv4         = aws_vpc.main.cidr_block
  ip_protocol       = "-1"

  tags = merge(local.common_tags, {
    Name = "secure-postgres-egress-vpc-only"
  })
}

###############################################################################
# Encryption key — customer-managed KMS with rotation
###############################################################################

resource "aws_kms_key" "rds" {
  description             = "CMK for PostgreSQL RDS storage, Performance Insights, and master secret"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = merge(local.common_tags, {
    Name = "secure-postgres-kms"
  })
}

###############################################################################
# Parameter group — enforce TLS connections
###############################################################################

resource "aws_db_parameter_group" "postgres" {
  name        = "secure-postgres-params"
  family      = "postgres16"
  description = "Hardened PostgreSQL parameters (TLS enforced)"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = merge(local.common_tags, {
    Name = "secure-postgres-params"
  })
}

###############################################################################
# PostgreSQL RDS instance
###############################################################################

resource "aws_db_instance" "postgres" {
  identifier     = "secure-postgres-db"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = "db.t3.medium"

  # Storage — encrypted at rest with CMK, gp3 with autoscaling headroom
  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.rds.arn

  # Credentials — no hardcoded password; RDS manages it in Secrets Manager
  db_name                       = "appdb"
  username                      = var.db_username
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.rds.arn

  # Networking — private subnets, no public access
  db_subnet_group_name   = aws_db_subnet_group.postgres.name
  vpc_security_group_ids = [aws_security_group.postgres.id]
  publicly_accessible    = false
  multi_az               = true
  port                   = 5432

  # Backups — 5-day retention, 03:00-06:00 window
  backup_retention_period = 5
  backup_window           = "03:00-06:00"
  maintenance_window      = "sun:07:00-sun:08:00"
  copy_tags_to_snapshot   = true
  skip_final_snapshot     = false
  final_snapshot_identifier = "secure-postgres-db-final-snapshot"

  # Hardening & observability
  parameter_group_name                  = aws_db_parameter_group.postgres.name
  deletion_protection                   = true
  auto_minor_version_upgrade            = true
  allow_major_version_upgrade           = false
  apply_immediately                     = false
  iam_database_authentication_enabled   = true
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.rds.arn
  performance_insights_retention_period = 7
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]
  ca_cert_identifier                    = "rds-ca-rsa2048-g1"

  tags = merge(local.common_tags, {
    Name = "secure-postgres-db"
  })
}

###############################################################################
# Outputs — no secret material exposed
###############################################################################

output "db_endpoint" {
  description = "Connection endpoint of the PostgreSQL instance"
  value       = aws_db_instance.postgres.endpoint
}

output "master_user_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the master credentials"
  value       = aws_db_instance.postgres.master_user_secret[0].secret_arn
}
```