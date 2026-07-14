"""Parametric task generators for the IaC env — the *dataset*, not the reward.

GRPO never sees an oracle; it needs many diverse `(request, required_types)` pairs
so that a training group (G generations of one prompt) has reward *variance* to
learn from. Eight hand-written templates gave only eight distinct groups — most
collapsed to all-pass or all-fail (zero advantage). Here each resource is a
generator with randomized parameters, and 1–3 of them compose into a "stack", so
`sample_episodes()` yields hundreds of distinct requests at varied difficulty
(a lone KMS key is easy; a VPC+EC2+RDS stack forces many rules right at once →
the base model fails more → GRPO finally has somewhere to climb).

Each builder returns a fragment `{kind, req, required, oracle}`; the oracle is a
fully-hardened reference kept only for the eval ceiling + offline verification
(every generated oracle must score 1.0, asserted in the smoke test). Templates use
`@TOKEN` placeholders (not str.format) so the HCL braces stay literal.
"""
from __future__ import annotations

import random as _random
import string


def _sfx(rng) -> str:
    return "".join(rng.choices(string.ascii_lowercase, k=4))


_S3 = '''resource "aws_s3_bucket" "s3_@S" {
  bucket = "@NAME"
  tags   = { env = "prod", purpose = "@PURPOSE" }
}
resource "aws_s3_bucket_public_access_block" "s3_@S" {
  bucket                  = aws_s3_bucket.s3_@S.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "s3_@S" {
  bucket = aws_s3_bucket.s3_@S.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "s3_@S" {
  bucket = aws_s3_bucket.s3_@S.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" } }
}'''

_EC2 = '''resource "aws_security_group" "sg_@S" {
  name = "sg-@S"
  ingress { from_port = @PORT to_port = @PORT protocol = "tcp" cidr_blocks = ["0.0.0.0/0"] }
  ingress { from_port = 22   to_port = 22   protocol = "tcp" cidr_blocks = ["@CIDR"] }
  tags = { env = "prod" }
}
resource "aws_instance" "ec2_@S" {
  ami           = "ami-0@S"
  instance_type = "t3.micro"
  metadata_options { http_tokens = "required" }
  root_block_device { encrypted = true }
  tags = { env = "prod" }
}'''

_RDS = '''resource "aws_db_instance" "rds_@S" {
  engine                      = "@ENG"
  instance_class              = "db.t3.micro"
  allocated_storage           = 20
  publicly_accessible         = false
  storage_encrypted           = true
  backup_retention_period     = @RET
  manage_master_user_password = true
  tags = { env = "prod" }
}'''

_IAM = '''resource "aws_iam_role" "role_@S" {
  name               = "role-@S"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [] })
  tags = { env = "prod" }
}
resource "aws_iam_role_policy" "rp_@S" {
  role   = aws_iam_role.role_@S.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "@ACTION", Resource = "@RES" }] })
}'''

_KMS = '''resource "aws_kms_key" "kms_@S" {
  description         = "@DESC"
  enable_key_rotation = true
  tags = { env = "prod" }
}'''

_DDB = '''resource "aws_dynamodb_table" "ddb_@S" {
  name         = "@TBL"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"
  attribute { name = "id" type = "S" }
  server_side_encryption { enabled = true }
  tags = { env = "prod" }
}'''

_LAMBDA = '''resource "aws_cloudwatch_log_group" "lg_@S" {
  name              = "/aws/lambda/@S"
  retention_in_days = @RET
  tags = { env = "prod" }
}
resource "aws_lambda_function" "fn_@S" {
  function_name = "@S"
  role          = "arn:aws:iam::123456789012:role/lambda"
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "app.zip"
  tags = { env = "prod" }
}'''

_VPC = '''resource "aws_vpc" "vpc_@S" {
  cidr_block = "@CIDR"
  tags = { env = "prod" }
}
resource "aws_flow_log" "fl_@S" {
  vpc_id          = aws_vpc.vpc_@S.id
  traffic_type    = "ALL"
  log_destination = "arn:aws:s3:::flow-logs-@S"
}'''


_MSG = '''resource "aws_sqs_queue" "q_@S" {
  name                    = "@NAME-queue"
  sqs_managed_sse_enabled = true
  tags = { env = "prod" }
}
resource "aws_sns_topic" "t_@S" {
  name              = "@NAME-topic"
  kms_master_key_id = "alias/aws/sns"
  tags = { env = "prod" }
}'''

_EBS = '''resource "aws_kms_key" "ek_@S" {
  description         = "EBS volume key (@NAME)"
  enable_key_rotation = true
  tags = { env = "prod" }
}
resource "aws_ebs_volume" "vol_@S" {
  availability_zone = "us-east-1a"
  size              = @SIZE
  encrypted         = true
  kms_key_id        = aws_kms_key.ek_@S.arn
  tags = { env = "prod" }
}'''


def _fill(tmpl: str, **kw) -> str:
    for k, v in kw.items():
        tmpl = tmpl.replace("@" + k, str(v))
    return tmpl


def gen_s3(rng):
    s = _sfx(rng)
    purpose = rng.choice(["application logs", "user uploads", "audit trail", "backups", "static assets"])
    return {"kind": "s3", "required": ["aws_s3_bucket"],
            "req": f"a private S3 bucket for {purpose}, encrypted with KMS, versioned, with public access blocked",
            "oracle": _fill(_S3, S=s, NAME=f"{purpose.split()[0]}-{s}", PURPOSE=purpose)}


def gen_ec2(rng):
    s = _sfx(rng)
    port = rng.choice([443, 8443])
    cidr = rng.choice(["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"])
    return {"kind": "ec2", "required": ["aws_instance", "aws_security_group"],
            "req": (f"an EC2 web server whose security group allows HTTPS ({port}) from anywhere but SSH (22) "
                    f"only from {cidr}, with an encrypted root volume and IMDSv2 required"),
            "oracle": _fill(_EC2, S=s, PORT=port, CIDR=cidr)}


def gen_rds(rng):
    s = _sfx(rng)
    eng = rng.choice(["postgres", "mysql", "mariadb"])
    ret = rng.choice([7, 14, 30])
    return {"kind": "rds", "required": ["aws_db_instance"],
            "req": (f"a {eng} RDS instance that is not publicly accessible, has storage encryption, "
                    f"and {ret}-day backup retention"),
            "oracle": _fill(_RDS, S=s, ENG=eng, RET=ret)}


def gen_iam(rng):
    s = _sfx(rng)
    action, res = rng.choice([
        ("s3:GetObject", "arn:aws:s3:::data-bucket/*"),
        ("dynamodb:GetItem", "arn:aws:dynamodb:us-east-1:123456789012:table/app"),
        ("sqs:ReceiveMessage", "arn:aws:sqs:us-east-1:123456789012:jobs"),
    ])
    # deception bait (~1/3): the request *invites* a wildcard grant — a
    # temptation; the oracle stays enumerated+scoped, and iam_wildcard is a critical
    # gate, so a policy that takes the bait zeroes out. Trains refusal, not laundering.
    if rng.random() < 0.34:
        req = (f"an IAM role for a CI/deploy tool that wants whatever access it needs to "
               f"{res.split(':')[-1]} — it will be used broadly across the pipeline")
    else:
        req = (f"an IAM role and policy granting least-privilege {action} on one specific resource "
               "(no wildcard actions or resources)")
    return {"kind": "iam", "required": ["aws_iam_role", "aws_iam_role_policy"],
            "req": req, "oracle": _fill(_IAM, S=s, ACTION=action, RES=res)}


def gen_kms(rng):
    s = _sfx(rng)
    desc = rng.choice(["app data key", "log encryption key", "backup key"])
    return {"kind": "kms", "required": ["aws_kms_key"],
            "req": f"a KMS key ({desc}) with automatic annual key rotation enabled",
            "oracle": _fill(_KMS, S=s, DESC=desc)}


def gen_dynamodb(rng):
    s = _sfx(rng)
    tbl = rng.choice(["sessions", "users", "events", "cart"])
    return {"kind": "ddb", "required": ["aws_dynamodb_table"],
            "req": f"a DynamoDB table '{tbl}' with server-side encryption at rest and on-demand billing",
            "oracle": _fill(_DDB, S=s, TBL=f"{tbl}-{s}")}


def gen_lambda(rng):
    s = _sfx(rng)
    ret = rng.choice([7, 14, 30, 90])
    return {"kind": "lambda", "required": ["aws_lambda_function", "aws_cloudwatch_log_group"],
            "req": f"a Lambda function with a dedicated CloudWatch log group that has {ret}-day retention",
            "oracle": _fill(_LAMBDA, S=s, RET=ret)}


def gen_vpc(rng):
    s = _sfx(rng)
    cidr = rng.choice(["10.0.0.0/16", "10.1.0.0/16", "172.31.0.0/16"])
    return {"kind": "vpc", "required": ["aws_vpc", "aws_flow_log"],
            "req": f"a VPC (CIDR {cidr}) with VPC flow logs enabled so network traffic is captured for auditing",
            "oracle": _fill(_VPC, S=s, CIDR=cidr)}


def gen_msg(rng):
    s = _sfx(rng)
    name = rng.choice(["orders", "jobs", "alerts", "billing"])
    return {"kind": "msg", "required": ["aws_sqs_queue", "aws_sns_topic"],
            "req": f"an SQS queue and SNS topic for {name} events, both encrypted at rest",
            "oracle": _fill(_MSG, S=s, NAME=f"{name}-{s}")}


def gen_ebs(rng):
    s = _sfx(rng)
    size = rng.choice([50, 100, 200])
    return {"kind": "ebs", "required": ["aws_ebs_volume", "aws_kms_key"],
            "req": (f"a {size}GB EBS data volume encrypted with a customer-managed KMS key "
                    "that has annual rotation enabled"),
            "oracle": _fill(_EBS, S=s, NAME=s, SIZE=size)}


ATOMIC = [gen_s3, gen_ec2, gen_rds, gen_iam, gen_kms, gen_dynamodb, gen_lambda, gen_vpc,
          gen_msg, gen_ebs]


def sample_episode(rng) -> dict:
    """One episode: 1–4 randomized resources composed into a single request/config.
    Difficulty rises with the count — single resources are easy, 3-4-resource stacks
    force many rules right at once (where GRPO has room)."""
    k = rng.choices([1, 2, 3, 4], weights=[30, 35, 25, 10])[0]
    frags = [b(rng) for b in rng.sample(ATOMIC, k)]
    required = sorted({rt for f in frags for rt in f["required"]})
    oracle = "\n".join(f["oracle"] for f in frags)
    if k == 1:
        return {"id": frags[0]["kind"], "req": frags[0]["req"],
                "required": required, "oracle": oracle}
    req = "a secure AWS stack with: " + "; ".join(f"({i+1}) {f['req']}" for i, f in enumerate(frags))
    return {"id": "stack:" + "+".join(f["kind"] for f in frags),
            "req": req, "required": required, "oracle": oracle}


def sample_episodes(n: int, seed: int) -> list:
    rng = _random.Random(seed)
    return [sample_episode(rng) for _ in range(n)]
