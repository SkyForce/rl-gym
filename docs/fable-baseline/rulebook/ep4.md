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
      Project     = var.app_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = "platform-team"
    }
  }
}

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "app_name" {
  description = "Name of the Elastic Beanstalk application."
  type        = string
  default     = "secure-web-app"
}

variable "environment" {
  description = "Environment tag applied to all resources."
  type        = string
  default     = "production"
}

data "aws_caller_identity" "current" {}

# Trust policy: only the Elastic Beanstalk service may assume this role,
# and only with the documented external id (confused-deputy protection).
data "aws_iam_policy_document" "beanstalk_assume" {
  statement {
    sid     = "AllowElasticBeanstalkAssumeRole"
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

# Least-privilege permissions for application version lifecycle cleanup:
# scoped to this application's versions and its own source bundle prefix only.
data "aws_iam_policy_document" "beanstalk_lifecycle" {
  statement {
    sid    = "DeleteExpiredApplicationVersions"
    effect = "Allow"

    actions = [
      "elasticbeanstalk:DeleteApplicationVersion",
    ]

    resources = [
      "arn:aws:elasticbeanstalk:${var.aws_region}:${data.aws_caller_identity.current.account_id}:applicationversion/${var.app_name}/*",
    ]
  }

  statement {
    sid    = "DeleteRetiredSourceBundles"
    effect = "Allow"

    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]

    resources = [
      "arn:aws:s3:::elasticbeanstalk-${var.aws_region}-${data.aws_caller_identity.current.account_id}/${var.app_name}/*",
    ]
  }

  statement {
    sid    = "ListSourceBundleBucket"
    effect = "Allow"

    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]

    resources = [
      "arn:aws:s3:::elasticbeanstalk-${var.aws_region}-${data.aws_caller_identity.current.account_id}",
    ]
  }
}

resource "aws_iam_role" "beanstalk_service" {
  name                 = "${var.app_name}-eb-service-role"
  description          = "Service role assumed by Elastic Beanstalk to enforce the application version lifecycle."
  assume_role_policy   = data.aws_iam_policy_document.beanstalk_assume.json
  max_session_duration = 3600

  tags = {
    Name = "${var.app_name}-eb-service-role"
  }
}

resource "aws_iam_role_policy" "beanstalk_lifecycle" {
  name   = "${var.app_name}-appversion-lifecycle"
  role   = aws_iam_role.beanstalk_service.id
  policy = data.aws_iam_policy_document.beanstalk_lifecycle.json
}

resource "aws_elastic_beanstalk_application" "this" {
  name        = var.app_name
  description = "Elastic Beanstalk application with a managed application version lifecycle."

  appversion_lifecycle {
    service_role          = aws_iam_role.beanstalk_service.arn
    max_count             = 25
    delete_source_from_s3 = true
  }

  tags = {
    Name = var.app_name
  }
}

output "application_name" {
  description = "Name of the Elastic Beanstalk application."
  value       = aws_elastic_beanstalk_application.this.name
}

output "service_role_arn" {
  description = "ARN of the Elastic Beanstalk lifecycle service role."
  value       = aws_iam_role.beanstalk_service.arn
}
```