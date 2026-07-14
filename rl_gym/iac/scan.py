"""Terraform security scan used as the verifiable reward for the IaC environment.

Uses Checkov if installed (the real thing — 1000+ policies); otherwise falls back
to a built-in rule set covering the most common findings (the handful that show up
in ~80% of unscanned repos: public buckets, missing encryption, world-open SSH,
hardcoded secrets, public RDS, IAM wildcards).

`scan()` returns pass/fail counts + findings (with severity); `resource_types()`
lists declared resources. Together they let the reward score *security posture*
AND check the config actually builds the requested infra — the gate that stops the
"empty config passes every check" reward-hack.
"""
from __future__ import annotations

import os
import re

_RESOURCE_RE = re.compile(r'resource\s+"([^"]+)"\s+"[^"]+"\s*\{')


def resource_types(hcl: str) -> list[str]:
    return _RESOURCE_RE.findall(hcl or "")


def resource_bodies(hcl: str) -> dict:
    """type -> list of block bodies (brace-matched). Lets gates check that a declared
    resource actually *configures* something — an empty stub `resource "t" "b" {}`
    satisfies the type regex but builds nothing (a syntactic bypass
    of a required-resources check)."""
    out: dict = {}
    for m in _RESOURCE_RE.finditer(hcl or ""):
        depth, i = 1, m.end()
        while i < len(hcl) and depth:
            if hcl[i] == "{":
                depth += 1
            elif hcl[i] == "}":
                depth -= 1
            i += 1
        out.setdefault(m.group(1), []).append(hcl[m.end():i - 1])
    return out


def _has(hcl, *names):
    return any(n in hcl for n in names)


# Each rule: id, severity, fn(hcl) -> "pass" | "fail" | "na" (na = not applicable).
# Applicable rules form the denominator; passes form the numerator.
def _r_s3_public(hcl):
    if "aws_s3_bucket" not in hcl: return "na"
    return "fail" if re.search(r'acl\s*=\s*"public-read', hcl) else "pass"

def _r_s3_encryption(hcl):
    if "aws_s3_bucket" not in hcl: return "na"
    return "pass" if re.search(r'server_side_encryption|sse_algorithm', hcl) else "fail"

def _r_s3_versioning(hcl):
    if "aws_s3_bucket" not in hcl: return "na"
    # value-checked: versioning must be ENABLED, not merely mentioned. Rejects the
    # aws_s3_bucket_versioning `status = "Suspended"` and inline `enabled = false` bypasses.
    if re.search(r'status\s*=\s*"Enabled"', hcl): return "pass"
    if re.search(r'versioning\s*\{[^{}]*enabled\s*=\s*true', hcl, re.S): return "pass"
    return "fail"

def _r_sg_world_admin(hcl):
    if "aws_security_group" not in hcl: return "na"
    # fail only if an admin port (SSH/RDP) is open to the world within the SAME ingress block
    for block in re.findall(r'ingress\s*\{[^{}]*\}', hcl, re.S):
        if "0.0.0.0/0" in block and re.search(r'from_port\s*=\s*(22|3389)\b', block):
            return "fail"
    return "pass"

def _r_rds_public(hcl):
    if "aws_db_instance" not in hcl: return "na"
    return "fail" if re.search(r'publicly_accessible\s*=\s*true', hcl) else "pass"

def _r_rds_encrypted(hcl):
    if "aws_db_instance" not in hcl: return "na"
    return "pass" if re.search(r'storage_encrypted\s*=\s*true', hcl) else "fail"

def _r_ebs_encrypted(hcl):
    if not _has(hcl, "root_block_device", "aws_ebs_volume", "ebs_block_device"): return "na"
    return "pass" if re.search(r'encrypted\s*=\s*true', hcl) else "fail"

def _r_secret(hcl):
    # literal password/secret assigned a string (not a var/ref) is a hardcoded secret
    return "fail" if re.search(r'(password|secret_key)\s*=\s*"(?!\$\{|var\.)[^"]+"', hcl) else "pass"

def _without_kms_policies(hcl: str) -> str:
    """Strip aws_kms_key / aws_kms_key_policy bodies before the IAM-wildcard checks. A KMS key
    policy's `Action = "kms:*"` + `Resource = "*"` scoped to the account root IS the AWS-
    recommended form (Resource "*" = this key), NOT an over-broad IAM grant. Flagging it was a
    false positive: the gap-mine loop's discovered kms_key_policy fix requires exactly this,
    so the fix tripped the critical no_critical gate and the model rationally learned to avoid
    key policies. Wildcards in REAL IAM policies (aws_iam_*, bucket policies) are untouched."""
    out, i = [], 0
    for m in re.finditer(r'resource\s+"aws_kms_key(?:_policy)?"\s+"[^"]*"\s*\{', hcl):
        out.append(hcl[i:m.start()])
        depth, j = 1, m.end()
        while j < len(hcl) and depth:
            if hcl[j] == "{":
                depth += 1
            elif hcl[j] == "}":
                depth -= 1
            j += 1
        i = j
    out.append(hcl[i:])
    return "".join(out)


def _r_iam_wildcard(hcl):
    # Full wildcard grants — scalar (`Action = "*"`) AND list (`actions = ["*"]`) forms.
    # Deliberately fires even when a `condition`/`Condition` block is present: LLM
    # "repairs" often launder wildcards behind irrelevant conditions that restrict the
    # principal but not the grant — a condition never cures a "*".
    h = _without_kms_policies(hcl)   # key-policy kms:* / Resource "*" is correct, not a grant
    if "aws_iam" not in h and "policy" not in h: return "na"
    if re.search(r'(?i)\b(action|resource)s?"?\s*[:=]\s*"\*"', h): return "fail"
    if re.search(r'(?i)\b(action|resource)s?"?\s*[:=]\s*\[[^\]]*"\*"', h): return "fail"
    return "pass"

def _r_iam_service_wildcard(hcl):
    # Service-level stars (`s3:*`, `glacier:*`) — not critical, but not least-privilege.
    # Our own v12 demo output used `actions = ["glacier:*"]` and scored 1.0; this rule
    # closes that seam (soft, HIGH — the model should learn to name specific actions).
    h = _without_kms_policies(hcl)   # exempt the standard KMS key-policy kms:* grant
    if "aws_iam" not in h and "policy" not in h: return "na"
    return "fail" if re.search(r'"[a-z0-9-]+:\*"', h) else "pass"

def _r_tags(hcl):
    if not resource_types(hcl): return "na"
    return "pass" if re.search(r'\btags\b', hcl) else "fail"

# --- harder rules: specific hardening the base model routinely omits (gives GRPO room) ---
def _r_s3_block_public(hcl):
    if "aws_s3_bucket" not in hcl: return "na"
    # value-checked: a public-access-block must have all four settings TRUE. The old rule
    # passed on mere presence, so `block_public_acls = false` slipped through — that's the gap.
    if not _has(hcl, "aws_s3_bucket_public_access_block", "block_public_acls"):
        return "fail"
    needed = ("block_public_acls", "block_public_policy",
              "ignore_public_acls", "restrict_public_buckets")
    return "pass" if all(re.search(rf'{k}\s*=\s*true', hcl) for k in needed) else "fail"

def _r_ec2_imdsv2(hcl):
    if "aws_instance" not in hcl: return "na"
    return "pass" if re.search(r'http_tokens\s*=\s*"required"', hcl) else "fail"

def _r_rds_backup(hcl):
    if "aws_db_instance" not in hcl: return "na"
    return "pass" if re.search(r'backup_retention_period\s*=\s*[1-9]', hcl) else "fail"

def _r_kms_rotation(hcl):
    if "aws_kms_key" not in hcl: return "na"
    return "pass" if re.search(r'enable_key_rotation\s*=\s*true', hcl) else "fail"

def _r_cw_retention(hcl):
    if "aws_cloudwatch_log_group" not in hcl: return "na"
    return "pass" if re.search(r'retention_in_days\s*=', hcl) else "fail"

def _r_ddb_encryption(hcl):
    if "aws_dynamodb_table" not in hcl: return "na"
    # value-checked: the SSE block must be enabled, not just present (rejects enabled = false).
    return "pass" if re.search(r'server_side_encryption\s*\{[^{}]*enabled\s*=\s*true', hcl, re.S) else "fail"

def _r_vpc_flow_logs(hcl):
    if "aws_vpc" not in hcl: return "na"
    return "pass" if "aws_flow_log" in hcl else "fail"


# --- drift rules: the "standard changed" set for the continual-learning demo -------
# Five Checkov-inspired policies (CKV_AWS_23/2_61/157/135/158) that v17 is NEVER
# rewarded on. Enabling RLGYM_IAC_DRIFT_RULES=1 simulates a real policy-release event:
# measure the drop, run one short continual update, prove recovery without forgetting.
# All soft severities — the drift story is about the component, not the gates.
def _r_sg_description(hcl):
    if "aws_security_group" not in hcl: return "na"
    return "pass" if re.search(r'description\s*=', hcl) else "fail"

def _r_s3_lifecycle(hcl):
    if "aws_s3_bucket" not in hcl: return "na"
    return "pass" if "lifecycle" in hcl else "fail"

def _r_rds_multi_az(hcl):
    if "aws_db_instance" not in hcl: return "na"
    return "pass" if re.search(r'multi_az\s*=\s*true', hcl) else "fail"

def _r_ebs_optimized(hcl):
    if "aws_instance" not in hcl: return "na"
    return "pass" if re.search(r'ebs_optimized\s*=\s*true', hcl) else "fail"

def _r_cw_log_kms(hcl):
    if "aws_cloudwatch_log_group" not in hcl: return "na"
    return "pass" if re.search(r'kms_key_id\s*=', hcl) else "fail"


DRIFT_RULES = [
    ("sg_description",   "low",    _r_sg_description),
    ("s3_lifecycle",     "medium", _r_s3_lifecycle),
    ("rds_multi_az",     "medium", _r_rds_multi_az),
    ("ec2_ebs_optimized","low",    _r_ebs_optimized),
    ("cw_log_kms",       "medium", _r_cw_log_kms),
]

# severity drives the hard "no_critical" gate vs the soft pass-rate component
RULES = [
    ("s3_public_acl",   "critical", _r_s3_public),
    ("sg_world_admin",  "critical", _r_sg_world_admin),
    ("rds_public",      "critical", _r_rds_public),
    ("hardcoded_secret","critical", _r_secret),
    ("iam_wildcard",    "critical", _r_iam_wildcard),
    ("iam_svc_wildcard","high",     _r_iam_service_wildcard),
    ("s3_encryption",   "high",     _r_s3_encryption),
    ("rds_encrypted",   "high",     _r_rds_encrypted),
    ("ebs_encrypted",   "high",     _r_ebs_encrypted),
    ("s3_block_public", "high",     _r_s3_block_public),
    ("ec2_imdsv2",      "high",     _r_ec2_imdsv2),
    ("ddb_encryption",  "high",     _r_ddb_encryption),
    ("s3_versioning",   "medium",   _r_s3_versioning),
    ("rds_backup",      "medium",   _r_rds_backup),
    ("kms_rotation",    "medium",   _r_kms_rotation),
    ("vpc_flow_logs",   "medium",   _r_vpc_flow_logs),
    ("cw_retention",    "low",      _r_cw_retention),
    ("tags",            "low",      _r_tags),
]

# --- generated rules: authored by a big model (rl_gym.gym.rulegen), validated against
# test cases + AST-sandboxed, staged here behind RLGYM_IAC_GENERATED_RULES=1 pending
# human review before promotion to the always-on set. The one below (rds_deletion_
# protection) was drafted by an open model and passed all its pass/fail examples.
def _r_rds_deletion_protection(hcl):
    starts = [m.end() for m in re.finditer(r'resource\s+"aws_db_instance"\s+"[^"]*"\s*\{', hcl)]
    if not starts:
        return "na"
    for start in starts:
        depth, i = 1, start
        while i < len(hcl) and depth > 0:
            if hcl[i] == "{": depth += 1
            elif hcl[i] == "}": depth -= 1
            i += 1
        if not re.search(r'\bdeletion_protection\s*=\s*true\b', hcl[start:i - 1]):
            return "fail"
    return "pass"


GENERATED_RULES = [
    ("rds_deletion_protection", "medium", _r_rds_deletion_protection),
]

# --- discovered rule: the self-improving loop found this from fw1's OWN traffic x Checkov
# (CKV2_AWS_64 "KMS key Policy is defined" — fired on ~26% of the model's configs, a genuine
# blind spot our 18 rules missed). Drafted by DeepSeek-V4-Pro, gated by AST + examples. Enabled
# behind RLGYM_IAC_DISCOVERED_RULES for the "discovered a blind spot -> retrained to close it" loop.
def _r_kms_key_policy(hcl):
    if 'resource "aws_kms_key"' not in hcl:
        return "na"
    for m in re.finditer(r'resource\s+"aws_kms_key"\s+"([^"]*)"\s*\{', hcl):
        name, start, depth, pos = m.group(1), m.end(), 1, m.end()
        while pos < len(hcl) and depth > 0:
            if hcl[pos] == "{": depth += 1
            elif hcl[pos] == "}": depth -= 1
            pos += 1
        body = hcl[start:pos - 1]
        inline = bool(re.search(r'\bpolicy\s*=', body))
        sep = bool(re.search(r'resource\s+"aws_kms_key_policy"\s+"[^"]*"\s*\{[^}]*key_id\s*=\s*aws_kms_key\.'
                             + re.escape(name) + r'\.', hcl, re.DOTALL))
        if not inline and not sep:
            return "fail"
    return "pass"


def repair_kms_policy(hcl: str) -> str:
    """Deterministic serving-layer repair for the discovered KMS blind spot: inject a standard
    AWS root key policy into any aws_kms_key that lacks one. This is the 'a boilerplate fix
    belongs in the verifier-guided repair turn, not the 8B's weights' resolution — six training
    attempts couldn't teach the verbose policy without collapsing the model, but the served
    system closes the gap for free. Idempotent: keys that already carry a policy are untouched;
    the `Resource="*"` here is exempt from the IAM-wildcard rule via _without_kms_policies."""
    if 'resource "aws_kms_key"' not in hcl:
        return hcl
    POLICY = ('\n  policy = jsonencode({\n'
              '    Version = "2012-10-17"\n'
              '    Statement = [{\n'
              '      Sid       = "EnableRootAccountAdmin"\n'
              '      Effect    = "Allow"\n'
              '      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }\n'
              '      Action    = "kms:*"\n'
              '      Resource  = "*"\n'
              '    }]\n'
              '  })\n')
    result, i, injected = [], 0, False
    for m in re.finditer(r'resource\s+"aws_kms_key"\s+"([^"]*)"\s*\{', hcl):
        name, start, depth, pos = m.group(1), m.end(), 1, m.end()
        while pos < len(hcl) and depth > 0:
            if hcl[pos] == "{": depth += 1
            elif hcl[pos] == "}": depth -= 1
            pos += 1
        close = pos - 1                          # index of this block's closing '}'
        body = hcl[start:close]
        inline = bool(re.search(r'\bpolicy\s*=', body))
        sep = bool(re.search(r'resource\s+"aws_kms_key_policy"\s+"[^"]*"\s*\{[^}]*key_id\s*=\s*aws_kms_key\.'
                             + re.escape(name) + r'\.', hcl, re.DOTALL))
        if inline or sep:
            continue                             # already has a policy — leave untouched
        result.append(hcl[i:close])
        result.append(POLICY)
        i = close
        injected = True
    result.append(hcl[i:])
    out = "".join(result)
    if injected and 'data "aws_caller_identity" "current"' not in out:
        out = 'data "aws_caller_identity" "current" {}\n\n' + out
    return out


DISCOVERED_RULES = [("kms_key_policy", "high", _r_kms_key_policy)]

_BASE_RULES = list(RULES)

# Actionable fix hint per rule — what a scanner finding should TELL you to do. A bare
# rule id ("ec2_ebs_optimized") is not something a model can reliably act on; the exact
# remediation is. Used by the repair loop's findings text (rl_gym.iac.repair) so the
# self-repair pass — and any human — knows the concrete change. Real scanners (Checkov)
# ship messages like these, not raw ids; matching that makes repair actually work.
FIX_HINTS = {
    "s3_public_acl":    'remove any acl = "public-read*"; keep the bucket private',
    "sg_world_admin":   "remove 0.0.0.0/0 ingress on port 22/3389; restrict to a private CIDR",
    "rds_public":       "set publicly_accessible = false on the aws_db_instance",
    "hardcoded_secret": "remove the literal password/secret; use manage_master_user_password = true or a variable",
    "iam_wildcard":     'replace any Action/Resource "*" with specific actions and ARNs',
    "iam_svc_wildcard": 'replace service wildcards like "s3:*" with the specific actions needed',
    "s3_encryption":    "add an aws_s3_bucket_server_side_encryption_configuration for the bucket",
    "rds_encrypted":    "set storage_encrypted = true on the aws_db_instance",
    "ebs_encrypted":    "set encrypted = true on the root_block_device / EBS volume",
    "s3_block_public":  "add an aws_s3_bucket_public_access_block with all four blocks true",
    "ec2_imdsv2":       'add metadata_options { http_tokens = "required" } to the aws_instance',
    "ddb_encryption":   "add server_side_encryption { enabled = true } to the aws_dynamodb_table",
    "s3_versioning":    "add an aws_s3_bucket_versioning resource with status = Enabled",
    "rds_backup":       "set backup_retention_period to at least 1 on the aws_db_instance",
    "kms_rotation":     "set enable_key_rotation = true on the aws_kms_key",
    "vpc_flow_logs":    "add an aws_flow_log for the VPC (traffic_type = ALL)",
    "cw_retention":     "set retention_in_days on the aws_cloudwatch_log_group",
    "tags":             "add a tags = { ... } block to the taggable resources",
    # drift rules (the 'policy update' set)
    "sg_description":   "add a description to the aws_security_group",
    "s3_lifecycle":     "add a lifecycle / aws_s3_bucket_lifecycle_configuration to the bucket",
    "rds_multi_az":     "set multi_az = true on the aws_db_instance",
    "ec2_ebs_optimized":"set ebs_optimized = true on the aws_instance",
    "cw_log_kms":       "set kms_key_id on the aws_cloudwatch_log_group for encryption at rest",
    # generated (rl_gym.gym.rulegen), validated:
    "rds_deletion_protection": "set deletion_protection = true on the aws_db_instance",
    # discovered (gap-miner: fw1 traffic x Checkov CKV2_AWS_64) + DeepSeek-V4-Pro drafted:
    "kms_key_policy": "add a policy to the aws_kms_key (inline policy = jsonencode({...}) or a separate aws_kms_key_policy resource)",
}

if os.environ.get("RLGYM_IAC_DRIFT_RULES"):
    RULES = RULES + DRIFT_RULES
    print(f"[iac.scan] DRIFT RULES ACTIVE: +{len(DRIFT_RULES)} rules "
          f"({', '.join(r[0] for r in DRIFT_RULES)}) — scanner now has {len(RULES)}")

if os.environ.get("RLGYM_IAC_GENERATED_RULES"):
    RULES = RULES + GENERATED_RULES
    _staged = []
    try:   # additional model-drafted, example-gated rules staged in generated_rules.py
        from .generated_rules import GENERATED_RULES as _staged, GENERATED_HINTS as _staged_hints
        RULES = RULES + list(_staged)
        FIX_HINTS.update(_staged_hints)
    except Exception as e:
        print(f"[iac.scan] staged generated_rules.py not loaded ({type(e).__name__}: {e})")
    print(f"[iac.scan] GENERATED RULES ACTIVE: +{len(GENERATED_RULES)} inline "
          f"+{len(_staged)} staged — pending human promotion")

if os.environ.get("RLGYM_IAC_DISCOVERED_RULES"):
    RULES = RULES + DISCOVERED_RULES
    print(f"[iac.scan] DISCOVERED RULES ACTIVE: +{len(DISCOVERED_RULES)} "
          f"({', '.join(r[0] for r in DISCOVERED_RULES)}) — gap-mined from traffic, gated")


def set_drift_rules(on: bool) -> None:
    """Runtime toggle (the webdemo's 'policy update' switch). scan() reads the module
    global at call time, so this affects reward + findings immediately. Callers must
    serialize (the demo flips it under its GPU lock); read RULES via the module
    attribute, not a from-import, to observe the switch."""
    global RULES
    RULES = _BASE_RULES + DRIFT_RULES if on else list(_BASE_RULES)


def _checkov(hcl: str):
    """Run real Checkov if available; return a scan dict or None to fall back."""
    try:
        import json, os, subprocess, tempfile
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "main.tf"), "w").write(hcl)
            out = subprocess.run(["checkov", "-d", d, "-o", "json", "--compact"],
                                 capture_output=True, text=True, timeout=60).stdout
        data = json.loads(out)
        res = data["results"] if isinstance(data, dict) else data[0]["results"]
        passed = len(res.get("passed_checks", []))
        failed_checks = res.get("failed_checks", [])
        findings = [(c.get("check_id", "?"), str(c.get("severity", "medium")).lower())
                    for c in failed_checks]
        applicable = passed + len(findings)
        return {"passed": passed, "failed": len(findings), "applicable": applicable,
                "findings": findings, "pass_rate": passed / applicable if applicable else 1.0,
                "engine": "checkov"}
    except Exception:
        return None


def scan(hcl: str) -> dict:
    ck = _checkov(hcl)
    if ck is not None:
        return ck
    findings, passed = [], 0
    for rid, sev, fn in RULES:
        v = fn(hcl)
        if v == "pass":
            passed += 1
        elif v == "fail":
            findings.append((rid, sev))
    applicable = passed + len(findings)
    return {"passed": passed, "failed": len(findings), "applicable": applicable,
            "findings": findings, "pass_rate": passed / applicable if applicable else 1.0,
            "engine": "builtin"}
