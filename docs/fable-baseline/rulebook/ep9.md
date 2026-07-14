```hcl
###############################################################################
# Secure RDS MySQL instance restored from an existing S3 backup (XtraBackup)
# - Encryption at rest (customer-managed KMS, rotation enabled)
# - No public access, private subnets, tightly scoped security group
# - No hardcoded secrets (master credentials managed by RDS/Secrets Manager)
# - Least-privilege IAM ingestion role scoped to the backup bucket/prefix
###############################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "db-restore-from-s3"
      Environment = "production"
      ManagedBy   = "terraform"
      DataClass   = "confidential"
    }
  }
}

###############################################################################
# Variables
###############################################################################

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "db_identifier" {
  description = "Identifier for the restored RDS instance"
  type        = string
  default     = "restored-mysql-db"
}

variable "vpc_id" {
  description = "ID of the existing VPC that hosts the database"
  type        = string
}

variable "db_subnet_ids" {
  description = "Private subnet IDs (at least two AZs) for the DB subnet group"
  type        = list(string)
}

variable "db_ingress_cidr" {
  description = "Internal CIDR allowed to reach MySQL (application tier only)"
  type        = string
  default     = "10.0.0.0/16"
}

variable "db_instance_class" {
  description = "Instance class for the restored database"
  type        = string
  default     = "db.m6i.large"
}

variable "backup_bucket_name" {
  description = "Name of the existing bucket holding the Percona XtraBackup files"
  type        = string
  default     = "example-org-mysql-backups"
}

variable "backup_s3_prefix" {
  description = "Key prefix of the backup files inside the backup bucket"
  type        = string
  default     = "backups/mysql/"
}

data "aws_caller_identity" "current" {}

###############################################################################
# KMS key for encryption at rest (rotation enabled, default key policy)
###############################################################################

resource "aws_kms_key" "rds" {
  description             = "CMK for RDS storage, Performance Insights and master credential encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = {
    Name = "${var.db_identifier}-kms"
  }
}

resource "aws_kms_alias" "rds" {
  name          = "alias/${var.db_identifier}"
  target_key_id = aws_kms_key.rds.key_id
}

###############################################################################
# Least-privilege IAM role RDS assumes to ingest the backup files
###############################################################################

resource "aws_iam_role" "rds_s3_import" {
  name = "${var.db_identifier}-s3-import-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowRDSServiceAssume"
        Effect = "Allow"
        Principal = {
          Service = "rds.amazonaws.com"
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })

  tags = {
    Name = "${var.db_identifier}-s3-import-role"
  }
}

resource "aws_iam_role_policy" "rds_s3_import_read" {
  name = "${var.db_identifier}-s3-import-read"
  role = aws_iam_role.rds_s3_import.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ListBackupBucketOnly"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = "arn:aws:s3:::${var.backup_bucket_name}"
      },
      {
        Sid    = "ReadBackupObjectsUnderPrefixOnly"
        Effect = "Allow"
        Action = [
          "s3:GetObject"
        ]
        Resource = "arn:aws:s3:::${var.backup_bucket_name}/${var.backup_s3_prefix}*"
      }
    ]
  })
}

###############################################################################
# Network placement: private subnets + tightly scoped security group
###############################################################################

resource "aws_db_subnet_group" "db" {
  name       = "${var.db_identifier}-subnets"
  subnet_ids = var.db_subnet_ids

  tags = {
    Name = "${var.db_identifier}-subnets"
  }
}

resource "aws_security_group" "db" {
  name        = "${var.db_identifier}-sg"
  description = "Allow MySQL only from the internal application CIDR; no egress"
  vpc_id      = var.vpc_id

  ingress {
    description = "MySQL from internal application tier only"
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = [var.db_ingress_cidr]
  }

  tags = {
    Name = "${var.db_identifier}-sg"
  }
}

###############################################################################
# Parameter group: enforce TLS for client connections
###############################################################################

resource "aws_db_parameter_group" "db" {
  name        = "${var.db_identifier}-params"
  family      = "mysql8.0"
  description = "Hardened parameters for the restored database"

  parameter {
    name  = "require_secure_transport"
    value = "1"
  }

  tags = {
    Name = "${var.db_identifier}-params"
  }
}

###############################################################################
# Pre-created log groups with retention for exported engine logs
###############################################################################

resource "aws_cloudwatch_log_group" "db_error" {
  name              = "/aws/rds/instance/${var.db_identifier}/error"
  retention_in_days = 90

  tags = {
    Name = "${var.db_identifier}-error-logs"
  }
}

resource "aws_cloudwatch_log_group" "db_general" {
  name              = "/aws/rds/instance/${var.db_identifier}/general"
  retention_in_days = 90

  tags = {
    Name = "${var.db_identifier}-general-logs"
  }
}

resource "aws_cloudwatch_log_group" "db_slowquery" {
  name              = "/aws/rds/instance/${var.db_identifier}/slowquery"
  retention_in_days = 90

  tags = {
    Name = "${var.db_identifier}-slowquery-logs"
  }
}

###############################################################################
# RDS instance restored from the S3 backup
###############################################################################

resource "aws_db_instance" "restored" {
  identifier     = var.db_identifier
  engine         = "mysql"
  engine_version = "8.0.36"
  instance_class = var.db_instance_class

  # Restore the database contents from the existing backup in S3
  s3_import {
    bucket_name           = var.backup_bucket_name
    bucket_prefix         = var.backup_s3_prefix
    ingestion_role        = aws_iam_role.rds_s3_import.arn
    source_engine         = "mysql"
    source_engine_version = "8.0.36"
  }

  # Storage: encrypted at rest with the customer-managed key
  allocated_storage     = 100
  max_allocated_storage = 500
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.rds.arn

  # Credentials: generated and stored by RDS in Secrets Manager (no literals)
  username                      = "dbadmin"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.rds.arn

  # Network: private only
  db_subnet_group_name   = aws_db_subnet_group.db.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = true
  port                   = 3306

  parameter_group_name                = aws_db_parameter_group.db.name
  iam_database_authentication_enabled = true

  # Backups and lifecycle protection
  backup_retention_period   = 7
  backup_window             = "03:00-04:00"
  maintenance_window        = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot     = true
  delete_automated_backups  = false
  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.db_identifier}-final"

  auto_minor_version_upgrade = true

  # Observability
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.rds.arn
  performance_insights_retention_period = 7
  enabled_cloudwatch_logs_exports       = ["error", "general", "slowquery"]

  tags = {
    Name = var.db_identifier
  }

  depends_on = [
    aws_cloudwatch_log_group.db_error,
    aws_cloudwatch_log_group.db_general,
    aws_cloudwatch_log_group.db_slowquery
  ]
}

###############################################################################
# Outputs
###############################################################################

output "db_instance_endpoint" {
  description = "Connection endpoint of the restored database"
  value       = aws_db_instance.restored.endpoint
}

output "db_instance_arn" {
  description = "ARN of the restored database instance"
  value       = aws_db_instance.restored.arn
}
```