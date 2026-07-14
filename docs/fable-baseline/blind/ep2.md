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

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project identifier used for naming and tagging."
  type        = string
  default     = "secure-ml"
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "prod"
}

variable "user_profile_name" {
  description = "Name of the SageMaker Studio user profile."
  type        = string
  default     = "data-scientist-1"
}

locals {
  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    DataClass   = "confidential"
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# ---------------------------------------------------------------------------
# KMS CMK for encryption at rest (domain EFS/EBS volumes), rotation enabled
# ---------------------------------------------------------------------------
resource "aws_kms_key" "sagemaker" {
  description             = "${var.project} SageMaker Studio encryption at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = merge(local.common_tags, { Name = "${var.project}-sagemaker-kms" })
}

resource "aws_kms_alias" "sagemaker" {
  name          = "alias/${var.project}-sagemaker"
  target_key_id = aws_kms_key.sagemaker.key_id
}

# ---------------------------------------------------------------------------
# Network: private VPC and subnet, no public IPs, no internet exposure
# ---------------------------------------------------------------------------
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, { Name = "${var.project}-vpc" })
}

resource "aws_subnet" "private" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false

  tags = merge(local.common_tags, { Name = "${var.project}-private-a" })
}

resource "aws_security_group" "studio" {
  name        = "${var.project}-sagemaker-studio"
  description = "SageMaker Studio ENIs: intra-SG and VPC-endpoint traffic only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Intra-security-group traffic (Studio apps, EFS, kernels)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  egress {
    description = "Intra-security-group traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  egress {
    description = "HTTPS to VPC endpoints inside the VPC only (no public internet)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  tags = merge(local.common_tags, { Name = "${var.project}-studio-sg" })
}

# ---------------------------------------------------------------------------
# IAM: least-privilege execution role with confused-deputy protection
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    sid     = "SageMakerAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "sagemaker_execution" {
  name               = "${var.project}-sagemaker-execution"
  description        = "Least-privilege execution role for SageMaker Studio users"
  assume_role_policy = data.aws_iam_policy_document.assume.json

  tags = merge(local.common_tags, { Name = "${var.project}-sagemaker-execution" })
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid    = "ScopedS3Access"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      "arn:aws:s3:::${var.project}-sagemaker-${data.aws_caller_identity.current.account_id}",
      "arn:aws:s3:::${var.project}-sagemaker-${data.aws_caller_identity.current.account_id}/*",
    ]
  }

  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/sagemaker/*",
    ]
  }

  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # This action does not support resource-level scoping
  }

  statement {
    sid    = "EcrPullScoped"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [
      "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/${var.project}-*",
    ]
  }

  statement {
    sid    = "KmsUseForSageMaker"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:GenerateDataKey*",
      "kms:CreateGrant",
    ]
    resources = [aws_kms_key.sagemaker.arn]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "${var.project}-sagemaker-least-privilege"
  role   = aws_iam_role.sagemaker_execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ---------------------------------------------------------------------------
# SageMaker domain: VPC-only access, CMK-encrypted, output sharing disabled
# ---------------------------------------------------------------------------
resource "aws_sagemaker_domain" "main" {
  domain_name             = "${var.project}-domain"
  auth_mode               = "IAM"
  vpc_id                  = aws_vpc.main.id
  subnet_ids              = [aws_subnet.private.id]
  app_network_access_type = "VpcOnly" # no direct internet access for Studio apps
  kms_key_id              = aws_kms_key.sagemaker.arn

  default_user_settings {
    execution_role  = aws_iam_role.sagemaker_execution.arn
    security_groups = [aws_security_group.studio.id]

    sharing_settings {
      notebook_output_option = "Disabled" # prevent notebook output leakage
    }
  }

  retention_policy {
    home_efs_file_system = "Retain"
  }

  tags = merge(local.common_tags, { Name = "${var.project}-domain" })
}

# ---------------------------------------------------------------------------
# SageMaker user profile
# ---------------------------------------------------------------------------
resource "aws_sagemaker_user_profile" "this" {
  domain_id         = aws_sagemaker_domain.main.id
  user_profile_name = var.user_profile_name

  user_settings {
    execution_role  = aws_iam_role.sagemaker_execution.arn
    security_groups = [aws_security_group.studio.id]

    sharing_settings {
      notebook_output_option = "Disabled"
    }
  }

  tags = merge(local.common_tags, { Name = var.user_profile_name })
}

output "sagemaker_domain_id" {
  description = "ID of the SageMaker domain."
  value       = aws_sagemaker_domain.main.id
}

output "sagemaker_user_profile_arn" {
  description = "ARN of the SageMaker user profile."
  value       = aws_sagemaker_user_profile.this.arn
}
```