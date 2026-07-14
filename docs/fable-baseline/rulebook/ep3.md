```hcl
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
  }
}

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "postgres-database"
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# Networking: custom VPC with private subnets (no IGW attached -> not routable
# from the internet), plus VPC flow logs to CloudWatch.
# ---------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "postgres-vpc"
  }
}

resource "aws_subnet" "db_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "${var.aws_region}a"
  map_public_ip_on_launch = false

  tags = {
    Name = "postgres-db-subnet-a"
  }
}

resource "aws_subnet" "db_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.2.0/24"
  availability_zone       = "${var.aws_region}b"
  map_public_ip_on_launch = false

  tags = {
    Name = "postgres-db-subnet-b"
  }
}

resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  name              = "/aws/vpc/postgres-vpc-flow-logs"
  retention_in_days = 365

  tags = {
    Name = "postgres-vpc-flow-logs"
  }
}

resource "aws_iam_role" "vpc_flow_logs" {
  name = "postgres-vpc-flow-logs-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "VPCFlowLogsAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "vpc-flow-logs.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "postgres-vpc-flow-logs-role"
  }
}

resource "aws_iam_role_policy" "vpc_flow_logs" {
  name = "postgres-vpc-flow-logs-policy"
  role = aws_iam_role.vpc_flow_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteVpcFlowLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          aws_cloudwatch_log_group.vpc_flow_logs.arn,
          "${aws_cloudwatch_log_group.vpc_flow_logs.arn}:log-stream:*"
        ]
      }
    ]
  })
}

resource "aws_flow_log" "main" {
  vpc_id          = aws_vpc.main.id
  traffic_type    = "ALL"
  iam_role_arn    = aws_iam_role.vpc_flow_logs.arn
  log_destination = aws_cloudwatch_log_group.vpc_flow_logs.arn

  tags = {
    Name = "postgres-vpc-flow-log"
  }
}

# ---------------------------------------------------------------------------
# Security group for the database: least privilege, VPC-internal traffic only.
# ---------------------------------------------------------------------------

resource "aws_security_group" "db" {
  name        = "postgres-db-sg"
  description = "Least-privilege security group for the PostgreSQL RDS instance"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "postgres-db-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "postgres_from_vpc" {
  security_group_id = aws_security_group.db.id
  description       = "PostgreSQL access from inside the VPC only"
  cidr_ipv4         = aws_vpc.main.cidr_block
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432

  tags = {
    Name = "postgres-ingress-5432-vpc-only"
  }
}

resource "aws_vpc_security_group_egress_rule" "https_within_vpc" {
  security_group_id = aws_security_group.db.id
  description       = "Restrict egress to HTTPS within the VPC (endpoints/monitoring)"
  cidr_ipv4         = aws_vpc.main.cidr_block
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443

  tags = {
    Name = "postgres-egress-443-vpc-only"
  }
}

# ---------------------------------------------------------------------------
# Encryption at rest: customer-managed KMS key with rotation enabled
# (default AWS key policy retained intentionally).
# ---------------------------------------------------------------------------

resource "aws_kms_key" "rds" {
  description             = "CMK for PostgreSQL RDS storage and secrets encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = {
    Name = "postgres-rds-kms-key"
  }
}

resource "aws_kms_alias" "rds" {
  name          = "alias/postgres-rds"
  target_key_id = aws_kms_key.rds.key_id
}

# ---------------------------------------------------------------------------
# RDS PostgreSQL: private subnets, encrypted storage, managed master password,
# TLS enforced, 5-day backups in the 03:00-06:00 window.
# ---------------------------------------------------------------------------

resource "aws_db_subnet_group" "postgres" {
  name        = "postgres-db-subnet-group"
  description = "Private subnets for the PostgreSQL instance"
  subnet_ids  = [aws_subnet.db_a.id, aws_subnet.db_b.id]

  tags = {
    Name = "postgres-db-subnet-group"
  }
}

resource "aws_db_parameter_group" "postgres" {
  name        = "postgres-ssl-required"
  family      = "postgres16"
  description = "Enforce TLS connections to PostgreSQL"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = {
    Name = "postgres-ssl-required"
  }
}

resource "aws_db_instance" "postgres" {
  identifier     = "postgres-primary"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = "db.m6g.large"

  allocated_storage     = 100
  max_allocated_storage = 500
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.rds.arn

  db_name  = "appdb"
  username = "dbadmin"
  port     = 5432

  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.rds.arn

  db_subnet_group_name   = aws_db_subnet_group.postgres.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name
  publicly_accessible    = false
  multi_az               = true

  backup_retention_period  = 5
  backup_window            = "03:00-06:00"
  maintenance_window       = "sun:07:00-sun:08:00"
  copy_tags_to_snapshot    = true
  delete_automated_backups = false

  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "postgres-primary-final-snapshot"

  auto_minor_version_upgrade          = true
  allow_major_version_upgrade         = false
  apply_immediately                   = false
  iam_database_authentication_enabled = true
  enabled_cloudwatch_logs_exports     = ["postgresql", "upgrade"]

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.rds.arn
  performance_insights_retention_period = 7

  tags = {
    Name = "postgres-primary"
  }
}

output "db_endpoint" {
  description = "Connection endpoint for the PostgreSQL instance"
  value       = aws_db_instance.postgres.endpoint
}
```