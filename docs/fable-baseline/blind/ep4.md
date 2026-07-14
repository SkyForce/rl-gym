```hcl
###############################################################################
# Secure Elastic Beanstalk application with an application-version lifecycle
# - Least-privilege, confused-deputy-protected IAM service role
# - Lifecycle rule prunes old versions and their S3 source bundles
#   (bundles live in the EB-managed bucket; enforce SSE on that bucket)
# - No hardcoded secrets; consistent tagging via provider default_tags
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
  region = var.aws_region

  # Tags applied to every resource created by this configuration
  default_tags {
    tags = {
      Project     = var.application_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = "platform-engineering"
    }
  }
}

###############################################################################
# Variables (no secrets — nothing sensitive is embedded in this configuration)
###############################################################################

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

variable "application_name" {
  description = "Name of the Elastic Beanstalk application"
  type        = string
  default     = "secure-eb-app"
}

variable "version_max_count" {
  description = "Maximum number of application versions to retain"
  type        = number
  default     = 25

  validation {
    condition     = var.version_max_count > 0
    error_message = "version_max_count must be a positive number."
  }
}

###############################################################################
# Account / partition / region context (avoids hardcoded ARNs)
###############################################################################

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  # Elastic Beanstalk stores application source bundles in this well-known,
  # account/region-scoped bucket. Policies below are scoped to it only.
  eb_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::elasticbeanstalk-${data.aws_region.current.name}-${data.aws_caller_identity.current.account_id}"
}

###############################################################################
# IAM service role for the application-version lifecycle (least privilege)
###############################################################################

# Trust policy: only the Elastic Beanstalk service may assume the role, and
# only with the documented ExternalId — confused-deputy protection.
data "aws_iam_policy_document" "eb_service_assume" {
  statement {
    sid     = "AllowElasticBeanstalkServiceAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["elasticbeanstalk.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = ["elasticbeanstalk"]
    }
  }
}

resource "aws_iam_role" "eb_service_role" {
  name                 = "${var.application_name}-appversion-lifecycle-role"
  description          = "Least-privilege service role used by Elastic Beanstalk to enforce the application version lifecycle"
  assume_role_policy   = data.aws_iam_policy_document.eb_service_assume.json
  max_session_duration = 3600

  tags = {
    Name = "${var.application_name}-appversion-lifecycle-role"
  }
}

# Permissions policy: only what the lifecycle needs — delete expired source
# bundles from the account's EB bucket. No wildcards on resources beyond the
# scoped bucket, no administrative actions.
data "aws_iam_policy_document" "eb_lifecycle_permissions" {
  statement {
    sid    = "ListElasticBeanstalkBucket"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [local.eb_bucket_arn]
  }

  statement {
    sid    = "DeleteExpiredSourceBundles"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["${local.eb_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "eb_lifecycle_permissions" {
  name   = "${var.application_name}-appversion-lifecycle-policy"
  role   = aws_iam_role.eb_service_role.id
  policy = data.aws_iam_policy_document.eb_lifecycle_permissions.json
}

###############################################################################
# Elastic Beanstalk application with version lifecycle
###############################################################################

resource "aws_elastic_beanstalk_application" "this" {
  name        = var.application_name
  description = "Secure Elastic Beanstalk application managed by Terraform with an enforced application version lifecycle"

  appversion_lifecycle {
    service_role          = aws_iam_role.eb_service_role.arn
    max_count             = var.version_max_count
    delete_source_from_s3 = true # prune stale bundles so old artifacts do not linger at rest
  }

  tags = {
    Name = var.application_name
  }

  # Ensure the role's permissions exist before EB validates/uses the lifecycle
  depends_on = [aws_iam_role_policy.eb_lifecycle_permissions]
}

###############################################################################
# Outputs (non-sensitive identifiers only)
###############################################################################

output "application_name" {
  description = "Name of the Elastic Beanstalk application"
  value       = aws_elastic_beanstalk_application.this.name
}

output "application_arn" {
  description = "ARN of the Elastic Beanstalk application"
  value       = aws_elastic_beanstalk_application.this.arn
}

output "lifecycle_service_role_arn" {
  description = "ARN of the IAM role used for the app version lifecycle"
  value       = aws_iam_role.eb_service_role.arn
}
```