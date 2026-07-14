# Frontier baseline: Claude Fable 5 on the IaC benchmark (static exhibit)

The same 10 real IaC-Eval dev episodes the models are scored on (dev[0:10] of the fixed
disjoint split), answered by **claude-fable-5** and judged by the same scanner + hard gates
as every row in the main README. Pass@1 protocol: one attempt per episode, no edits after
scoring. Generated 2026-07-06..09 via fresh Claude Code subagents (empty context, tool use
forbidden), so the API safety classifier was not in the path.

Re-score offline (no GPU, no API key): `python scripts/score_fable_baseline.py`

## Three conditions

| condition | reward | gate rate | what it models |
|---|---|---|---|
| `blind/` — episode prompt only | 0.598 | 70% | first-contact frontier model |
| `rulebook/` — + `rulebook.txt` (~580 tok) | **1.000** | **100%** | agent with learned memory of the verifier |
| (not in repo) scanner's author, same session | 0.950 | 100% | upper-bound sanity check |
| GRPO v16 8B, for reference (n=120) | 0.737 | 85.8% | the trained open model |

**Blind** Fable lands at base-8B level: 3/10 episodes zeroed by the `no_critical` gate — the
canonical KMS key policy (`Action="kms:*"`, `Resource="*"`; also flagged by Checkov
CKV_AWS_109), an unscopeable `ecr:GetAuthorizationToken` on `Resource="*"`, wildcard actions
inside Deny statements — plus flow logs omitted on 6/10 VPCs. Sophisticated infra, penalized
by a context-free audit it has never seen.

**Rulebook** Fable is perfect. The rulebook is a distillation of the verifier — the rule list,
the gates, and the gotchas the blind run was burned by. That is the point of the exhibit:

1. The knowledge that closes the 0.60 → 1.00 gap *is the verifier*. No judge, no rulebook,
   no learning signal, no proof of conformance — true for frontier agents and small models alike.
2. A saturated judge means the judge is the bottleneck: rule coverage (full Checkov, OPA/Rego)
   is the axis that re-differentiates generators, which is why verifier depth is the product.
3. Caveats: n=10 (±~0.13 SE); rulebook written with knowledge of the blind failures
   (steady-state memory, not cold start); Claude Code path has no API refusal classifier.

## Cost model (measured sizes, July 2026 prices)

Token counts measured from these files (~3.8 chars/token), +~1,000 assumed thinking tokens
(always-on and billed as output on Fable). GPU rates from nebius.com/prices (June 2026):
L40S-AMD ≈ $1.55/h all-in, H100 SXM $3.85/h. 8B timings as observed in the live L40S demo
(eager mode, bf16 — a tuned serving stack would be faster).

| serving option | per request | notes |
|---|---|---|
| Fable API, blind | ~$0.17 | 138 in + ~2,370 out + ~1,000 thinking @ $10/$50 per MTok |
| Fable API + rulebook | ~$0.16 | +582 input tokens ≈ +$0.006; outputs ran slightly leaner |
| tuned 8B, pass@1 | ~$0.005 | ~6 s GPU time |
| tuned 8B, pass@1 + self-repair (default) | ~$0.006 | ~12 s; within ~2% of best-of-4+repair (dev 0.865 / holdout 0.963) |
| tuned 8B, best-of-4 + repair (fallback) | ~$0.013 | ~30 s; quality-max, edges ahead on holdout (0.981) |

- **Gap: 12–33× per request.** The rulebook barely moves API cost — output/thinking tokens
  dominate, and those are structural on Fable.
- **Break-even for a dedicated L40S** ($1,132/mo): ~7,000 requests/mo (~230/day). Below that,
  pay-per-token is cheaper; above it — CI/CD hooks, PR bots, fleet scanning — the owned GPU
  wins, and it runs inside the customer's VPC.
- **Training is noise in the economics:** a full SFT+GRPO run is ~3 H100-hours ≈ $12.
