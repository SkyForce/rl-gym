```hcl
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "ec2-daily-backup"
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# KMS customer-managed key (AWS default key policy; rotation enabled)
# Used to encrypt both the EC2 root EBS volume and the Backup vault.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "backup" {
  description             = "CMK for EC2 EBS volume and AWS Backup vault encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = {
    Name = "ec2-backup-cmk"
  }
}

resource "aws_kms_alias" "backup" {
  name          = "alias/ec2-daily-backup"
  target_key_id = aws_kms_key.backup.key_id
}

# ---------------------------------------------------------------------------
# Latest Amazon Linux 2023 AMI via public SSM parameter
# ---------------------------------------------------------------------------
data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# ---------------------------------------------------------------------------
# IAM role + instance profile for the EC2 instance (SSM management, no SSH)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "ec2_ssm" {
  name = "ec2-backup-demo-instance-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEC2AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "ec2-backup-demo-instance-role"
  }
}

resource "aws_iam_role_policy_attachment" "ec2_ssm_core" {
  role       = aws_iam_role.ec2_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2" {
  name = "ec2-backup-demo-instance-profile"
  role = aws_iam_role.ec2_ssm.name

  tags = {
    Name = "ec2-backup-demo-instance-profile"
  }
}

# ---------------------------------------------------------------------------
# EC2 instance: encrypted root volume, IMDSv2 enforced, no public IP
# ---------------------------------------------------------------------------
resource "aws_instance" "app" {
  ami                         = data.aws_ssm_parameter.al2023_ami.value
  instance_type               = "t3.micro"
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  associate_public_ip_address = false
  monitoring                  = true
  ebs_optimized               = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20
    encrypted             = true
    kms_key_id            = aws_kms_key.backup.arn
    delete_on_termination = true

    tags = {
      Name = "ec2-backup-demo-root-volume"
    }
  }

  tags = {
    Name   = "ec2-backup-demo-instance"
    Backup = "daily"
  }
}

# ---------------------------------------------------------------------------
# IAM role assumed by the AWS Backup service (AWS managed service-role policies)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "backup" {
  name = "aws-backup-service-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowBackupServiceAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "backup.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "aws-backup-service-role"
  }
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "restore" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores"
}

# ---------------------------------------------------------------------------
# AWS Backup: encrypted vault (with vault lock), daily-at-midnight plan,
# and a selection scoped to exactly the one EC2 instance
# ---------------------------------------------------------------------------
resource "aws_backup_vault" "this" {
  name        = "ec2-daily-backup-vault"
  kms_key_arn = aws_kms_key.backup.arn

  tags = {
    Name = "ec2-daily-backup-vault"
  }
}

resource "aws_backup_vault_lock_configuration" "this" {
  backup_vault_name  = aws_backup_vault.this.name
  min_retention_days = 7
  max_retention_days = 365
}

resource "aws_backup_plan" "daily" {
  name = "ec2-daily-midnight-backup-plan"

  rule {
    rule_name         = "daily-midnight-backup"
    target_vault_name = aws_backup_vault.this.name
    schedule          = "cron(0 0 * * ? *)"
    start_window      = 60
    completion_window = 300

    lifecycle {
      cold_storage_after = 30
      delete_after       = 365
    }

    recovery_point_tags = {
      Name   = "ec2-daily-recovery-point"
      Source = "aws-backup"
    }
  }

  tags = {
    Name = "ec2-daily-midnight-backup-plan"
  }
}

resource "aws_backup_selection" "ec2" {
  name         = "ec2-instance-selection"
  iam_role_arn = aws_iam_role.backup.arn
  plan_id      = aws_backup_plan.daily.id

  resources = [
    aws_instance.app.arn,
  ]
}
```