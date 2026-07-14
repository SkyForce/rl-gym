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
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "secure-sagemaker-studio"
      Environment = "prod"
      ManagedBy   = "terraform"
      Owner       = "ml-platform-team"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# KMS key for encryption at rest (rotation enabled; AWS default key policy
# is retained intentionally rather than a custom policy)
# ---------------------------------------------------------------------------

resource "aws_kms_key" "sagemaker" {
  description             = "CMK for SageMaker Studio domain home-directory (EFS) encryption at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = {
    Name = "sagemaker-studio-cmk"
  }
}

resource "aws_kms_alias" "sagemaker" {
  name          = "alias/sagemaker-studio"
  target_key_id = aws_kms_key.sagemaker.key_id
}

# ---------------------------------------------------------------------------
# Network: private VPC + private subnet (no public IPs), flow logs enabled
# ---------------------------------------------------------------------------

resource "aws_vpc" "sagemaker" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "sagemaker-studio-vpc"
  }
}

resource "aws_subnet" "sagemaker_private" {
  vpc_id                  = aws_vpc.sagemaker.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "us-east-1a"
  map_public_ip_on_launch = false

  tags = {
    Name = "sagemaker-studio-private-a"
  }
}

resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  name              = "/vpc/sagemaker-studio/flow-logs"
  retention_in_days = 365

  tags = {
    Name = "sagemaker-vpc-flow-logs"
  }
}

resource "aws_iam_role" "vpc_flow_logs" {
  name = "sagemaker-vpc-flow-logs-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowVPCFlowLogsAssume"
        Effect = "Allow"
        Principal = {
          Service = "vpc-flow-logs.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "sagemaker-vpc-flow-logs-role"
  }
}

resource "aws_iam_role_policy" "vpc_flow_logs" {
  name = "sagemaker-vpc-flow-logs-delivery"
  role = aws_iam_role.vpc_flow_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowFlowLogDeliveryToLogGroup"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          aws_cloudwatch_log_group.vpc_flow_logs.arn,
          "${aws_cloudwatch_log_group.vpc_flow_logs.arn}:*"
        ]
      }
    ]
  })
}

resource "aws_flow_log" "sagemaker_vpc" {
  vpc_id                   = aws_vpc.sagemaker.id
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.vpc_flow_logs.arn
  iam_role_arn             = aws_iam_role.vpc_flow_logs.arn
  max_aggregation_interval = 60

  tags = {
    Name = "sagemaker-studio-vpc-flow-log"
  }
}

# ---------------------------------------------------------------------------
# Security group for the Studio domain: intra-SG traffic only, no internet
# ingress; egress restricted to HTTPS toward VPC endpoints in the VPC CIDR
# ---------------------------------------------------------------------------

resource "aws_security_group" "sagemaker_domain" {
  name        = "sagemaker-studio-domain-sg"
  description = "SageMaker Studio domain (VpcOnly) - intra-SG traffic only"
  vpc_id      = aws_vpc.sagemaker.id

  ingress {
    description = "NFS between Studio apps and the EFS home directories within this SG"
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    self        = true
  }

  ingress {
    description = "Studio inter-app (JupyterServer to KernelGateway) traffic within this SG"
    from_port   = 8192
    to_port     = 65535
    protocol    = "tcp"
    self        = true
  }

  egress {
    description = "Intra-SG TCP return traffic"
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    self        = true
  }

  egress {
    description = "HTTPS to VPC interface endpoints only"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.sagemaker.cidr_block]
  }

  tags = {
    Name = "sagemaker-studio-domain-sg"
  }
}

# ---------------------------------------------------------------------------
# Least-privilege SageMaker execution role (scoped resources, no wildcards)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "sagemaker_execution" {
  name = "sagemaker-studio-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowSageMakerAssume"
        Effect = "Allow"
        Principal = {
          Service = "sagemaker.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "sagemaker-studio-execution-role"
  }
}

resource "aws_iam_role_policy" "sagemaker_execution" {
  name = "sagemaker-studio-least-privilege"
  role = aws_iam_role.sagemaker_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "StudioAppLogging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/sagemaker/studio",
          "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/sagemaker/studio:*"
        ]
      },
      {
        Sid    = "UseDomainCmkOnly"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey",
          "kms:CreateGrant"
        ]
        Resource = [
          aws_kms_key.sagemaker.arn
        ]
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# SageMaker Studio domain: VpcOnly (no public internet path), CMK-encrypted
# home directories, notebook output sharing disabled
# ---------------------------------------------------------------------------

resource "aws_sagemaker_domain" "studio" {
  domain_name             = "secure-sagemaker-studio"
  auth_mode               = "IAM"
  vpc_id                  = aws_vpc.sagemaker.id
  subnet_ids              = [aws_subnet.sagemaker_private.id]
  app_network_access_type = "VpcOnly"
  kms_key_id              = aws_kms_key.sagemaker.arn

  default_user_settings {
    execution_role  = aws_iam_role.sagemaker_execution.arn
    security_groups = [aws_security_group.sagemaker_domain.id]

    sharing_settings {
      notebook_output_option = "Disabled"
    }
  }

  tags = {
    Name = "secure-sagemaker-studio"
  }
}

# ---------------------------------------------------------------------------
# SageMaker user profile bound to the hardened domain
# ---------------------------------------------------------------------------

resource "aws_sagemaker_user_profile" "data_scientist" {
  domain_id         = aws_sagemaker_domain.studio.id
  user_profile_name = "data-scientist-01"

  user_settings {
    execution_role  = aws_iam_role.sagemaker_execution.arn
    security_groups = [aws_security_group.sagemaker_domain.id]

    sharing_settings {
      notebook_output_option = "Disabled"
    }
  }

  tags = {
    Name = "data-scientist-01"
  }
}
```