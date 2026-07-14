# Verifier rule backlog — mapping the scanner to CIS/Checkov, and the next ~20 rules

The verifier grows as a **curated, severity-tiered subset of the CIS-AWS / Checkov policy
families**, expressed as fast deterministic predicates. This is the prioritized backlog: what
we cover now, what to harden, and the next rules as ready-to-draft `rulegen` specs.

**Tiering (decides where a rule runs):**
- **value** — a scalar attribute equals a required value (`encrypted = true`). Fast → **reward**.
- **per_resource** — brace-matched to a specific resource body / block. Fast → **reward**.
- **presence** — a keyword appears (weakest; gameable). Upgrade these, don't add more.
- **relational / semantic** — needs the resource graph or policy semantics (bucket ↔ its
  policy, IAM action+resource pairing). Regex can't do these reliably → **ensemble gate only**
  (Checkov graph / OPA), never the fast reward.

---

## A. What the scanner covers today (18 built-in + 5 drift + 1 generated)

| Category | Rules (severity) |
|---|---|
| Public exposure / network | `s3_public_acl`(crit), `rds_public`(crit), `sg_world_admin`(crit), `s3_block_public`(high) |
| Encryption at rest | `s3_encryption`(high), `rds_encrypted`(high), `ebs_encrypted`(high), `ddb_encryption`(high), `kms_rotation`(med) |
| IAM / least privilege | `iam_wildcard`(crit), `iam_svc_wildcard`(high) |
| Secrets | `hardcoded_secret`(crit) |
| Compute hardening | `ec2_imdsv2`(high) |
| Logging / audit | `vpc_flow_logs`(med), `cw_retention`(low) |
| Resilience | `rds_backup`(med), `s3_versioning`(med), `rds_deletion_protection`(med, generated) |
| Hygiene | `tags`(low) |
| Drift set (demo) | `sg_description`, `s3_lifecycle`, `rds_multi_az`, `ec2_ebs_optimized`, `cw_log_kms` |

**Gaps vs CIS-AWS:** in-transit encryption, most audit controls (CloudTrail), several at-rest
types (EFS/Redshift/SNS/SQS), DB-port exposure, subnet/instance public IPs, EKS endpoint, PITR.

---

## B. Harden existing (the value-blind rules flagged in review — do these first)

These already exist but check *presence not value* → gameable. Tighten before adding new ones.
**Changing them changes the reward → re-eval required.**

| Rule | Current gap | Fix |
|---|---|---|
| `s3_block_public` | passes if `block_public_acls` *appears*, even `= false` | require all four `*_public_*` = `true` |
| `s3_versioning` | passes on the substring `versioning`, incl. `status = "Suspended"` | require `status = "Enabled"` |
| `ddb_encryption` | passes on `server_side_encryption` keyword, incl. `enabled = false` | require `enabled = true` |

---

## C. New — Batch 1: fast reward rules (15 specs, ready to draft, validated)

Written to `rl_gym/iac/data/rulespecs/*.json`. Each is a `rulegen` **input** (intent +
pass/fail examples); a big model drafts the predicate, then AST + these examples gate it.
Three were validated end-to-end (`efs_encrypted`, `redshift_public`, `sg_open_database_ports`
→ all ACCEPT).

| Rule | Sev | Tier | Category | ≈ Checkov |
|---|---|---|---|---|
| `sg_open_database_ports` | critical | per_resource | network | SG family |
| `redshift_public` | high | value | network | CKV_AWS_87 |
| `eks_no_public_endpoint` | high | per_resource | network | CKV_AWS_39 |
| `ec2_no_public_ip` | medium | value | network | CKV_AWS_88 |
| `subnet_no_public_ip` | medium | value | network | CKV_AWS_130 |
| `efs_encrypted` | high | value | encryption | CKV_AWS_42 |
| `redshift_encrypted` | high | value | encryption | CKV_AWS_64 |
| `sns_encrypted` | medium | value | encryption | CKV_AWS_26 |
| `sqs_encrypted` | medium | value | encryption | CKV_AWS_27 |
| `cloudtrail_encrypted` | high | value | logging | CKV_AWS_35 |
| `cloudtrail_multi_region` | medium | value | logging | CKV_AWS_67 |
| `cloudtrail_log_validation` | medium | value | logging | CKV_AWS_36 |
| `dynamodb_pitr` | medium | per_resource | resilience | CKV_AWS_28 |
| `rds_iam_auth` | low | value | resilience | CKV_AWS_161 |
| `elb_https_only` | high | per_resource | transit | CKV_AWS_2/103 → **gate** (HTTP-redirect FP) |

*(Checkov IDs are best-effort — confirm against your pinned Checkov version before shipping.)*

---

## D. New — Batch 2: relational / semantic → ensemble gate only (don't force into regex)

These need the resource graph or policy semantics; keep them in the offline Checkov/OPA gate,
not the fast reward. Listed so the coverage plan is complete:

- `s3_enforce_tls` — a bucket *policy* Denies `aws:SecureTransport = false` (bucket ↔ policy link)
- `s3_access_logging` — a `logging` config targets *this* bucket
- `iam_no_passrole_wildcard` — `iam:PassRole` paired with `Resource "*"` (action+resource semantics)
- `iam_no_wildcard_principal` — `Principal "*"` in a resource policy without a scoping `Condition`
- `lambda_no_plaintext_secrets` — secret-looking literals in `environment` (fuzzy; human triage)
- `cloudtrail_to_cloudwatch` — trail wired to a CW log group (cross-resource)

---

## How to use

```bash
# draft one rule via a big model on Token Factory, gated by its examples:
python -m rl_gym.gym.rulegen --spec rl_gym/iac/data/rulespecs/efs_encrypted.json \
    --model Qwen/Qwen3-235B-A22B --out /tmp/efs_encrypted.py

# or validate a hand-written / mined candidate (no API):
python -m rl_gym.gym.rulegen --spec <spec.json> --candidate <fn.py>
```

Accepted **reward-tier** rules get appended to `rl_gym/iac/scan.RULES` behind human review
(the `GENERATED_RULES` staging pattern). **Gate-tier** rules stay in the ensemble
(Checkov/OPA) and feed the gap-miner, never the inner-loop reward.

**Order of work:** (B) harden the 3 value-blind rules → (C) merge Batch 1 fast rules → re-eval
the frozen anchors (`EVAL_ONLY=1`, ~$1–2) → continual update if the reward shifted → (D) wire the
ensemble gate for the relational set. Every reward change is followed by a re-eval; that's the
ratchet.
