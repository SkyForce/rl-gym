```hcl
###############################################################################
# Secure S3 Glacier vault with a least-privilege access policy and a
# compliance Vault Lock (WORM retention).
#
# Security notes:
# - Amazon S3 Glacier encrypts all archives at rest with AES-256 by default;
#   no additional configuration is required or exposed by the service.
# - Access policy grants only the required Glacier actions to this account
#   (no public "*" principal Allow), and explicitly denies non-TLS access.
# - No hardcoded account IDs, regions, or secrets — resolved via data sources.
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
}

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "vault_name" {
  description = "Name of the Glacier vault"
  type        = string
  default     = "secure-archive-vault"
}

variable "retention_days" {
  description = "Minimum archive retention period enforced by the vault lock"
  type        = number
  default     = 365
}

locals {
  # Construct the vault ARN up front so the vault's own access policy can
  # reference it without a circular dependency.
  vault_arn = "arn:aws:glacier:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:vaults/${var.vault_name}"

  common_tags = {
    Environment = "production"
    ManagedBy   = "terraform"
    Project     = "secure-archive"
    DataClass   = "confidential"
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# Least-privilege vault ACCESS policy:
# - Allow only this account's principals (governed further by their IAM
#   policies) to perform the minimal set of archive operations.
# - Deny any request made over an insecure (non-TLS) transport.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "vault_access" {
  statement {
    sid    = "AllowAccountLeastPrivilegeArchiveOps"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    actions = [
      "glacier:UploadArchive",
      "glacier:InitiateJob",
      "glacier:GetJobOutput",
      "glacier:ListJobs",
      "glacier:DescribeVault",
    ]

    resources = [local.vault_arn]
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = ["glacier:*"]
    resources = [local.vault_arn]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# ---------------------------------------------------------------------------
# Glacier vault (server-side encryption at rest is enabled by default, AES-256)
# ---------------------------------------------------------------------------
resource "aws_glacier_vault" "this" {
  name          = var.vault_name
  access_policy = data.aws_iam_policy_document.vault_access.json

  tags = merge(local.common_tags, {
    Name = var.vault_name
  })
}

# ---------------------------------------------------------------------------
# Vault Lock compliance policy: deny deletion of archives younger than the
# retention period, for all principals (including administrators).
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "vault_lock" {
  statement {
    sid    = "DenyDeleteBeforeRetentionPeriod"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = ["glacier:DeleteArchive"]
    resources = [aws_glacier_vault.this.arn]

    condition {
      test     = "NumericLessThanEquals"
      variable = "glacier:ArchiveAgeInDays"
      values   = [tostring(var.retention_days)]
    }
  }
}

# ---------------------------------------------------------------------------
# Lock the policy. complete_lock = true makes the lock immutable once applied
# (irreversible WORM protection). ignore_deletion_error is left at its safe
# default (false) so a locked vault cannot be silently dropped from state.
# ---------------------------------------------------------------------------
resource "aws_glacier_vault_lock" "this" {
  vault_name    = aws_glacier_vault.this.name
  policy        = data.aws_iam_policy_document.vault_lock.json
  complete_lock = true
}

output "vault_arn" {
  description = "ARN of the Glacier vault"
  value       = aws_glacier_vault.this.arn
}

output "vault_lock_state" {
  description = "Whether the vault lock has been fully engaged"
  value       = aws_glacier_vault_lock.this.complete_lock
}
```