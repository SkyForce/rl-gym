```hcl
###############################################################################
# Secure RDS MySQL instance restored from Percona XtraBackup files in S3
# (RestoreDBInstanceFromS3 via the aws_db_instance `s3_import` block)
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
}

#------------------------------------------------------------------------------
# Input variables (no secrets — master credentials are generated and stored
# in AWS Secrets Manager by RDS itself)
#------------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name used for resource naming and tagging."
  type        = string
  default     = "db-restore"
}

variable "environment" {
  description = "Deployment environment tag."
  type        = string
  default     = "prod"
}

variable "backup_bucket_name" {
  description = "Existing S3 bucket containing the Percona XtraBackup backup files."
  type        = string

  validation {
    condition     = length(var.backup_bucket_name) > 0
    error_message = "backup_bucket_name must not be empty."
  }
}

variable "backup_bucket_prefix" {
  description = "Prefix (folder) within the bucket where the backup files live."
  type        = string
  default     = "mysql-backups/"
}

variable "engine_version" {
  description = "MySQL engine version for the restored instance."
  type        = string
  default     = "8.0.36"
}

variable "source_engine_version" {
  description = "MySQL version the S3 backup was taken from."
  type        = string
  default     = "8.0.32"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.m6g.large"
}

locals {
  name = "${var.project}-${var.environment}"

  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    DataClass   = "confidential"
  }
}

#------------------------------------------------------------------------------
# Existing backup bucket (must already exist; used to scope IAM least privilege)
#------------------------------------------------------------------------------

data "aws_s3_bucket" "backup" {
  bucket = var.backup_bucket_name
}

data "aws_availability_zones" "available" {
  state = "available"
}

#------------------------------------------------------------------------------
# KMS CMK for encryption at rest (storage, Performance Insights, master secret)
#------------------------------------------------------------------------------

resource "aws_kms_key" "rds" {
  description             = "CMK for ${local.name} RDS storage, PI and master-user secret"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = merge(local.common_tags, { Name = "${local.name}-rds-kms" })
}

resource "aws_kms_alias" "rds" {
  name          = "alias/${local.name}-rds"
  target_key_id = aws_kms_key.rds.key_id
}

#------------------------------------------------------------------------------
# Network isolation: private subnets only, no public IPs, no internet gateway
#------------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, { Name = "${local.name}-vpc" })
}

resource "aws_subnet" "db" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false

  tags = merge(local.common_tags, { Name = "${local.name}-db-private-${count.index}" })
}

resource "aws_db_subnet_group" "main" {
  name        = "${local.name}-db-subnets"
  description = "Private subnets for ${local.name} RDS"
  subnet_ids  = aws_subnet.db[*].id

  tags = merge(local.common_tags, { Name = "${local.name}-db-subnets" })
}

# Application tier SG — attach to workloads that legitimately need DB access.
resource "aws_security_group" "app" {
  name_prefix = "${local.name}-app-"
  description = "Application tier allowed to reach the database"
  vpc_id      = aws_vpc.main.id

  tags = merge(local.common_tags, { Name = "${local.name}-app-sg" })

  lifecycle {
    create_before_destroy = true
  }
}

# Database SG — no rules except MySQL from the app SG; no egress (deny all).
resource "aws_security_group" "db" {
  name_prefix = "${local.name}-db-"
  description = "RDS MySQL - ingress restricted to application tier only"
  vpc_id      = aws_vpc.main.id

  tags = merge(local.common_tags, { Name = "${local.name}-db-sg" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
  description                  = "MySQL from application tier only"

  tags = local.common_tags
}

#------------------------------------------------------------------------------
# Least-privilege IAM role RDS assumes to read the backup from S3
#------------------------------------------------------------------------------

data "aws_iam_policy_document" "rds_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "s3_import" {
  name_prefix        = "${local.name}-s3-import-"
  description        = "Allows RDS to read XtraBackup files for restore-from-S3"
  assume_role_policy = data.aws_iam_policy_document.rds_assume.json

  tags = merge(local.common_tags, { Name = "${local.name}-s3-import-role" })
}

data "aws_iam_policy_document" "s3_import" {
  statement {
    sid       = "ListBackupBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [data.aws_s3_bucket.backup.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.backup_bucket_prefix}*"]
    }
  }

  statement {
    sid       = "ReadBackupObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${data.aws_s3_bucket.backup.arn}/${var.backup_bucket_prefix}*"]
  }
}

resource "aws_iam_role_policy" "s3_import" {
  name_prefix = "${local.name}-s3-import-"
  role        = aws_iam_role.s3_import.id
  policy      = data.aws_iam_policy_document.s3_import.json
}

# Enhanced monitoring role
data "aws_iam_policy_document" "monitoring_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name_prefix        = "${local.name}-rds-mon-"
  assume_role_policy = data.aws_iam_policy_document.monitoring_assume.json

  tags = merge(local.common_tags, { Name = "${local.name}-rds-monitoring-role" })
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

#------------------------------------------------------------------------------
# Parameter group enforcing TLS in transit
#------------------------------------------------------------------------------

resource "aws_db_parameter_group" "mysql" {
  name_prefix = "${local.name}-mysql8-"
  family      = "mysql8.0"
  description = "Enforce TLS for ${local.name}"

  parameter {
    name  = "require_secure_transport"
    value = "1"
  }

  tags = merge(local.common_tags, { Name = "${local.name}-mysql8-params" })

  lifecycle {
    create_before_destroy = true
  }
}

#------------------------------------------------------------------------------
# RDS MySQL instance restored from the S3 backup
#------------------------------------------------------------------------------

resource "aws_db_instance" "from_s3" {
  identifier     = "${local.name}-mysql"
  engine         = "mysql"
  engine_version = var.engine_version
  instance_class = var.db_instance_class

  # Restore source: Percona XtraBackup files in S3
  s3_import {
    bucket_name           = data.aws_s3_bucket.backup.id
    bucket_prefix         = var.backup_bucket_prefix
    ingestion_role        = aws_iam_role.s3_import.arn
    source_engine         = "mysql"
    source_engine_version = var.source_engine_version
  }

  # Storage — encrypted at rest with a customer-managed KMS key
  allocated_storage     = 100
  max_allocated_storage = 500
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.rds.arn

  # Credentials — generated and rotated by RDS, stored in Secrets Manager,
  # encrypted with the CMK. Nothing hardcoded, nothing in state as plaintext.
  username                      = "dbadmin"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.rds.arn

  # Network — private subnets, no public access, SG-scoped ingress only
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = true
  port                   = 3306

  # Hardening and access control
  parameter_group_name                = aws_db_parameter_group.mysql.name
  iam_database_authentication_enabled = true
  ca_cert_identifier                  = "rds-ca-rsa2048-g1"
  auto_minor_version_upgrade          = true
  allow_major_version_upgrade         = false
  apply_immediately                   = false
  deletion_protection                 = true

  # Backup and recovery
  backup_retention_period   = 14
  backup_window             = "02:00-03:00"
  maintenance_window        = "sun:04:00-sun:05:00"
  copy_tags_to_snapshot     = true
  delete_automated_backups  = false
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-mysql-final"

  # Observability
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.rds.arn
  performance_insights_retention_period = 7
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.rds_monitoring.arn
  enabled_cloudwatch_logs_exports       = ["audit", "error", "general", "slowquery"]

  tags = merge(local.common_tags, { Name = "${local.name}-mysql" })

  depends_on = [
    aws_iam_role_policy.s3_import,
    aws_iam_role_policy_attachment.rds_monitoring,
  ]
}

#------------------------------------------------------------------------------
# Outputs
#------------------------------------------------------------------------------

output "db_instance_arn" {
  description = "ARN of the restored RDS instance."
  value       = aws_db_instance.from_s3.arn
}

output "db_endpoint" {
  description = "Connection endpoint of the restored database."
  value       = aws_db_instance.from_s3.endpoint
}

output "master_user_secret_arn" {
  description = "Secrets Manager ARN holding the RDS-managed master credentials."
  value       = aws_db_instance.from_s3.master_user_secret[0].secret_arn
}

output "app_security_group_id" {
  description = "Attach this SG to application workloads that need DB access."
  value       = aws_security_group.app.id
}
```