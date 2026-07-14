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
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "secure-msk"
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# KMS key for encryption at rest (default key policy retained; rotation on)
# ---------------------------------------------------------------------------
resource "aws_kms_key" "msk" {
  description             = "CMK for MSK cluster encryption at rest and log encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = {
    Name = "msk-encryption-key"
  }
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "msk-vpc"
  }
}

resource "aws_subnet" "private_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "us-east-1a"
  map_public_ip_on_launch = false

  tags = {
    Name = "msk-private-a"
    Tier = "private"
  }
}

resource "aws_subnet" "private_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.2.0/24"
  availability_zone       = "us-east-1b"
  map_public_ip_on_launch = false

  tags = {
    Name = "msk-private-b"
    Tier = "private"
  }
}

resource "aws_subnet" "private_c" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.3.0/24"
  availability_zone       = "us-east-1c"
  map_public_ip_on_launch = false

  tags = {
    Name = "msk-private-c"
    Tier = "private"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "msk-igw"
  }
}

# Private route table: implicit local route only, no default route to the
# internet gateway, keeping broker subnets fully private.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "msk-private-rt"
  }
}

resource "aws_route_table_association" "private_a" {
  subnet_id      = aws_subnet.private_a.id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "private_b" {
  subnet_id      = aws_subnet.private_b.id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "private_c" {
  subnet_id      = aws_subnet.private_c.id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------------------
# VPC flow logs
# ---------------------------------------------------------------------------
resource "aws_flow_log" "vpc" {
  vpc_id               = aws_vpc.main.id
  traffic_type         = "ALL"
  log_destination_type = "s3"
  log_destination      = "arn:aws:s3:::example-org-vpc-flow-log-archive"

  tags = {
    Name = "msk-vpc-flow-logs"
  }
}

# ---------------------------------------------------------------------------
# Security groups (least privilege, rules managed as dedicated resources)
# ---------------------------------------------------------------------------
resource "aws_security_group" "msk_broker" {
  name        = "msk-broker-sg"
  description = "Controls access to MSK broker ENIs"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "msk-broker-sg"
  }
}

resource "aws_security_group" "msk_client" {
  name        = "msk-client-sg"
  description = "Attached to Kafka client applications permitted to reach the cluster"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "msk-client-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "broker_tls_from_clients" {
  security_group_id            = aws_security_group.msk_broker.id
  description                  = "Kafka TLS listener from authorized client security group"
  ip_protocol                  = "tcp"
  from_port                    = 9094
  to_port                      = 9094
  referenced_security_group_id = aws_security_group.msk_client.id

  tags = {
    Name = "msk-ingress-tls"
  }
}

resource "aws_vpc_security_group_ingress_rule" "broker_iam_from_clients" {
  security_group_id            = aws_security_group.msk_broker.id
  description                  = "Kafka SASL IAM listener from authorized client security group"
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098
  referenced_security_group_id = aws_security_group.msk_client.id

  tags = {
    Name = "msk-ingress-iam"
  }
}

resource "aws_vpc_security_group_ingress_rule" "broker_intra_cluster" {
  security_group_id            = aws_security_group.msk_broker.id
  description                  = "Broker to broker communication within the cluster"
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.msk_broker.id

  tags = {
    Name = "msk-ingress-intra-cluster"
  }
}

resource "aws_vpc_security_group_egress_rule" "broker_intra_cluster" {
  security_group_id            = aws_security_group.msk_broker.id
  description                  = "Broker to broker communication within the cluster"
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.msk_broker.id

  tags = {
    Name = "msk-egress-intra-cluster"
  }
}

resource "aws_vpc_security_group_egress_rule" "client_to_broker_tls" {
  security_group_id            = aws_security_group.msk_client.id
  description                  = "Client egress to Kafka TLS listener"
  ip_protocol                  = "tcp"
  from_port                    = 9094
  to_port                      = 9094
  referenced_security_group_id = aws_security_group.msk_broker.id

  tags = {
    Name = "msk-client-egress-tls"
  }
}

resource "aws_vpc_security_group_egress_rule" "client_to_broker_iam" {
  security_group_id            = aws_security_group.msk_client.id
  description                  = "Client egress to Kafka SASL IAM listener"
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098
  referenced_security_group_id = aws_security_group.msk_broker.id

  tags = {
    Name = "msk-client-egress-iam"
  }
}

# ---------------------------------------------------------------------------
# Broker logging
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "msk_broker_logs" {
  name              = "/aws/msk/secure-msk-cluster/broker-logs"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.msk.arn

  tags = {
    Name = "msk-broker-log-group"
  }
}

# ---------------------------------------------------------------------------
# MSK cluster: KMS encryption at rest, TLS in transit, IAM auth, private only
# ---------------------------------------------------------------------------
resource "aws_msk_cluster" "main" {
  cluster_name           = "secure-msk-cluster"
  kafka_version          = "3.6.0"
  number_of_broker_nodes = 3
  enhanced_monitoring    = "PER_BROKER"

  broker_node_group_info {
    instance_type = "kafka.m5.large"

    client_subnets = [
      aws_subnet.private_a.id,
      aws_subnet.private_b.id,
      aws_subnet.private_c.id,
    ]

    security_groups = [aws_security_group.msk_broker.id]

    connectivity_info {
      public_access {
        type = "DISABLED"
      }
    }
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = aws_kms_key.msk.arn

    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  client_authentication {
    unauthenticated = false

    sasl {
      iam = true
    }
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk_broker_logs.name
      }
    }
  }

  tags = {
    Name = "secure-msk-cluster"
  }
}
```