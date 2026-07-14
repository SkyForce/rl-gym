```hcl
###############################################################################
# Secure S3 Glacier vault with a least-privilege access policy and a
# compliance vault lock (WORM). Glacier encrypts all archives at rest by
# default with AES-256 managed by the service.
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

variable "aws_region" {
  description = "AWS region for the Glacier vault"
  type        = string
  default     = "us-east-1"
}

variable "vault_name" {
  description = "Name of the Glacier vault"
  type        = string
  default     = "secure-archive-vault"
}

variable "min_archive_retention_days" {
  description = "Minimum number of days an archive must be retained before deletion is allowed"
  type        = number
  default     = 365
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "secure-archive"
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}

# Identity of the owning account (used to scope principals and build ARNs;
# avoids any wildcard principals or resources).
data "aws_caller_identity" "current" {}

locals {
  vault_arn = "arn:aws:glacier:${var.aws_region}:${data.aws_caller_identity.current.account_id}:vaults/${var.vault_name}"
}

###############################################################################
# Vault access policy — least privilege:
# only principals in this account, only enumerated Glacier actions,
# only on this specific vault ARN.
###############################################################################
data "aws_iam_policy_document" "vault_access" {
  statement {
    sid    = "AllowAccountScopedArchiveOperations"
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
      "glacier:GetVaultAccessPolicy",
      "glacier:GetVaultLock",
      "glacier:GetVaultNotifications",
      "glacier:ListMultipartUploads",
      "glacier:ListParts",
      "glacier:ListTagsForVault",
    ]

    resources = [local.vault_arn]
  }
}

###############################################################################
# Vault lock policy — deny early archive deletion (compliance/WORM control),
# scoped to account principals and this vault only.
###############################################################################
data "aws_iam_policy_document" "vault_lock" {
  statement {
    sid    = "DenyArchiveDeletionBeforeRetentionExpires"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    actions = [
      "glacier:DeleteArchive",
    ]

    resources = [local.vault_arn]

    condition {
      test     = "NumericLessThan"
      variable = "glacier:ArchiveAgeInDays"
      values   = [tostring(var.min_archive_retention_days)]
    }
  }
}

###############################################################################
# Glacier vault (archives are encrypted at rest with AES-256 by default)
###############################################################################
resource "aws_glacier_vault" "this" {
  name          = var.vault_name
  access_policy = data.aws_iam_policy_document.vault_access.json

  tags = {
    Name           = var.vault_name
    DataClass      = "archive"
    RetentionDays  = tostring(var.min_archive_retention_days)
  }
}

###############################################################################
# Vault lock — locks the retention policy in place (irreversible once complete)
###############################################################################
resource "aws_glacier_vault_lock" "this" {
  vault_name    = aws_glacier_vault.this.name
  complete_lock = true
  policy        = data.aws_iam_policy_document.vault_lock.json
}

output "glacier_vault_arn" {
  description = "ARN of the locked Glacier vault"
  value       = aws_glacier_vault.this.arn
}

output "glacier_vault_name" {
  description = "Name of the Glacier vault"
  value       = aws_glacier_vault.this.name
}
```