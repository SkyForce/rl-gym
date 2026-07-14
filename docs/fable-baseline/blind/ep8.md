```hcl
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
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "ec2-daily-backup"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# Variables (no hardcoded secrets or account-specific values)
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment tag"
  type        = string
  default     = "production"
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.micro"
}

variable "backup_retention_days" {
  description = "Number of days to retain daily recovery points"
  type        = number
  default     = 35
}

# ---------------------------------------------------------------------------
# AMI lookup (latest Amazon Linux 2023, owned by Amazon)
# ---------------------------------------------------------------------------

data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ---------------------------------------------------------------------------
# KMS CMK for encryption at rest (EBS volume + Backup vault), with rotation
# ---------------------------------------------------------------------------

resource "aws_kms_key" "main" {
  description             = "CMK encrypting the EC2 root volume and AWS Backup vault"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = {
    Name = "ec2-backup-cmk"
  }
}

resource "aws_kms_alias" "main" {
  name          = "alias/ec2-daily-backup"
  target_key_id = aws_kms_key.main.key_id
}

# ---------------------------------------------------------------------------
# Networking: private-only placement, no public IPs, deny-all ingress
# ---------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "ec2-backup-vpc"
  }
}

resource "aws_subnet" "private" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = false

  tags = {
    Name = "ec2-backup-private-subnet"
  }
}

resource "aws_security_group" "instance" {
  name        = "ec2-backup-instance-sg"
  description = "No inbound access; HTTPS egress only"
  vpc_id      = aws_vpc.main.id

  # No ingress blocks: all inbound traffic is denied.

  egress {
    description = "Allow outbound HTTPS for OS updates and AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "ec2-backup-instance-sg"
  }
}

# ---------------------------------------------------------------------------
# EC2 instance: encrypted root volume, IMDSv2 enforced, no public IP
# ---------------------------------------------------------------------------

resource "aws_instance" "app" {
  ami                         = data.aws_ami.amazon_linux.id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.private.id
  vpc_security_group_ids      = [aws_security_group.instance.id]
  associate_public_ip_address = false
  monitoring                  = true
  ebs_optimized               = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # enforce IMDSv2
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20
    encrypted             = true
    kms_key_id            = aws_kms_key.main.arn
    delete_on_termination = true
  }

  tags = {
    Name   = "ec2-backup-demo-instance"
    Backup = "daily"
  }
}

# ---------------------------------------------------------------------------
# IAM role for AWS Backup (least privilege: service trust + managed
# backup-only policy, no restore/admin permissions attached)
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "backup_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name               = "ec2-daily-backup-service-role"
  assume_role_policy = data.aws_iam_policy_document.backup_assume_role.json

  tags = {
    Name = "ec2-daily-backup-service-role"
  }
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

# ---------------------------------------------------------------------------
# AWS Backup: encrypted vault, daily plan at midnight, instance selection
# ---------------------------------------------------------------------------

resource "aws_backup_vault" "main" {
  name        = "ec2-daily-backup-vault"
  kms_key_arn = aws_kms_key.main.arn

  tags = {
    Name = "ec2-daily-backup-vault"
  }
}

# Vault lock (governance mode) protects recovery points from early deletion.
resource "aws_backup_vault_lock_configuration" "main" {
  backup_vault_name  = aws_backup_vault.main.name
  min_retention_days = 7
  max_retention_days = 365
}

resource "aws_backup_plan" "daily" {
  name = "ec2-daily-midnight-plan"

  rule {
    rule_name         = "daily-midnight-backup"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 0 * * ? *)" # every day at 00:00 UTC
    start_window      = 60
    completion_window = 180

    lifecycle {
      delete_after = var.backup_retention_days
    }
  }

  tags = {
    Name = "ec2-daily-midnight-plan"
  }
}

resource "aws_backup_selection" "ec2" {
  name         = "ec2-instance-selection"
  iam_role_arn = aws_iam_role.backup.arn
  plan_id      = aws_backup_plan.daily.id

  # Scope the selection to exactly one instance (least privilege).
  resources = [aws_instance.app.arn]
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "instance_id" {
  description = "ID of the backed-up EC2 instance"
  value       = aws_instance.app.id
}

output "backup_plan_id" {
  description = "ID of the daily backup plan"
  value       = aws_backup_plan.daily.id
}

output "backup_vault_arn" {
  description = "ARN of the encrypted backup vault"
  value       = aws_backup_vault.main.arn
}
```