# rl-gym — Audited RLVR for Agents

**Train small open models against verifiable rewards — with the reward-hacking audit built in.**
Submission to the Nebius Serverless AI Builders Challenge (2026).

A reusable post-training platform: **SFT warm-start → GRPO (RLVR) → eval → reward-hacking
audit → serve → mine traffic ↺**, where the *task* is a pluggable `Environment`. Two loops
close it — served traffic becomes the next model, and mined logs × external checkers grow the
**verifier** itself (a big reasoning model drafts new rules, gated by tests + a human review).
The `Environment` retargets to any verifiable-reward domain:

| Vertical | Verifiable reward | Hard gates |
|---|---|---|
| `iac` — **secure Terraform** | security scanner pass-rate (Checkov-style rules) | `builds_required`, `no_critical` |
| *(roadmap)* K8s hardening · CI policy | same shape: scanner pass-rate | same shape: hard gates |

The thesis follows the RLVR decision rule: *if the task is verifiable, apply GRPO — and be
careful about reward hacking.* rl-gym makes the second half first-class: hard gates that zero
gamed rewards, saturation caps, and an audit (uniq%, per-component breakdown) that shows *how*
the model improved, not just that a number went up.

## Headline results (granite-4.1-8b, Nebius H100, N=120, clean disjoint split)

**Base model chosen empirically** — a bake-off mode scored four candidates on the real benchmark
before training: granite-4.1-8b 0.636 > Qwen3-8B 0.567 > Qwen2.5-Coder-7B 0.494 > Qwen2.5-7B 0.440.

**Training ladder — synthetic dev, 120 episodes** (LoRA, fp32 SFT -> GRPO vs the hardened
scanner + 2026-07-11 generator: 1-4-resource stacks, wildcard-bait prompts):

```
policy               reward     gate     valid
random                0.428    100.0%   100.0%
base LLM              0.468     48.3%   100.0%
SFT (warm-start)      0.654     69.2%   100.0%
GRPO (RLVR)           0.809     85.0%   100.0%
oracle (ceiling)      1.000    100.0%   100.0%
```

**Reward-hacking audit — clean.** Security component climbs monotonically (0.842 -> 0.881 -> 0.913)
with uniq% flat at 93.3% for all three models: no output collapse, no single-rule gaming, gates held.

**Real-world eval — [IaC-Eval](https://huggingface.co/datasets/autoiac-project/iac-eval)
(120 human-written requests, frozen dev split, fully disjoint from training):**

```
policy               reward     gate
random                0.480    100.0%
base LLM              0.629     76.7%
SFT (warm-start)      0.679     80.8%
GRPO (RLVR)           0.694     81.7%
human reference       0.459     85.0%
```

**Holdout eval — mined episodes (Checkov test corpus + GitHub modules) from a source
training NEVER samples, hash-disjoint split (83 episodes):**

```
policy               reward     gate
random                0.493    100.0%
base LLM              0.756     84.3%
SFT (warm-start)      0.781     84.3%
GRPO (RLVR)           0.876     92.8%
human reference       0.524     75.9%
```

The claims, at N=120/83 (±0.04): the trained 8B scores **1.5x the human-written references** on
security; on the fully-virgin holdout source **GRPO adds +0.12 reward and +8.5pt gate rate over
base** (and +0.10 over SFT — the RL stage, not imitation, carries the transfer); and the audit
shows *how*: a balanced component lift with flat output diversity, not a gamed one.

**Integrity, quantified — this is the audit culture working on ourselves.** An earlier run (v16)
reported 0.737 on IaC-Eval with ~2/3 of dev prompts accidentally present in its training mix
(prompt exposure only; the reward never reads references, so label leakage was impossible). We
froze a disjoint split, re-ran the identical recipe, and measured the truth: 0.694 — **the leak
was worth +0.04 and is now corrected everywhere**. The same promotion ratchet also caught a
plausible-sounding upgrade (RAFT-style SFT on the model's own verifier-perfect samples) silently
*regressing* out-of-distribution (0.579 vs 0.679 SFT — rejection sampling over-selects easy
episodes); it was blocked from promotion. Negative results are kept on purpose: a platform that
sells reward-hacking audits should be seen catching its own.

### The flywheel — serving traffic becomes the next model, gated by a program

The loop the platform is built on, run end to end with no human in the decision: the live
demo logs every served request (real phrasing) and every self-repair transcript (config +
findings + outcome) to S3; an aggregator pools that traffic and mixes in replay; a continual
update trains from the incumbent; and an **executable promotion gate** (`rl_gym/gym/gate.py`)
evaluates the candidate against the incumbent on the frozen anchors and exits PROMOTE or BLOCK.

First real cycle (26 pilot requests → 38 logged episodes → 50-step update → gate, ~$4):

```
                       incumbent (v18)   flywheel candidate   verdict
real dev  (n=60)        0.697 / 83.3%     0.738 / 88.3%       +0.041
holdout   (n=60)        0.859 / 91.7%     0.906 / 93.3%       +0.047
                                          collapse alarm: none  → PROMOTE
```

Serving traffic literally produced a better model, and a program — not a person — cleared it
to ship. A cycle that *fails* the anchors is auto-blocked and the incumbent stays — "reliable"
is the gate, not a hope.

**Where the gain lands — and why best-of-n stopped mattering.** The flywheel's lift is almost
entirely on *pass@1*, not best-of-n (`scripts/flywheel_stats.py`, n=60 anchors):

```
              pass@1  Δ      best-of-4  Δ
dev    v18 → fw1  +0.041         +0.012
holdout           +0.047         -0.014
```

Training on the model's own repair transcripts sharpens its *single-shot* output, collapsing the
pass@1↔best-of-4 gap (dev 0.079→0.050, holdout 0.094→0.033). Once the floor rises to the ceiling,
blind resampling has little left to find.

**Serving 2×2 on the anchors** (`scripts/serve_2x2.py`, fw1, n=60): repair helps pass@1 far more
than best-of-4 (it has more to fix; best-of-4 already skims the cleanest of 4 samples), so the
two served systems land within ~2% of each other:

```
                 dev-60           holdout-60
                 +repair          +repair
pass@1            0.865            0.963
best-of-4         0.854            0.981
```

pass@1+repair edges ahead on dev (+0.011), best-of-4+repair on holdout (−0.017) — a wash at ±0.03
noise. Since **pass@1 + self-repair costs ~40% of best-of-4 + repair** (2 sequential generations
vs 5) at statistically equal quality, it's the serving default; best-of-n + repair is kept as the
quality-max fallback (it edges ahead on the harder OOD anchor). The verifier's role shifts from
*selector* to *instructor*, and self-improvement moves quality from the sampling budget into the
weights.

### Continual learning — the drift demo (measured, not aspirational)

Security standards drift; the pipeline adapts. We added 5 Checkov-inspired rules the model was
never rewarded on ("the standard changed"), measured the drop, and ran ONE 50-step continual
update (~1 GPU-hour, ~$1.3) from the existing checkpoint:

```
                                  new scanner    old scanner
GRPO   (pre-drift)                   0.667          0.682
GRPO+1 continual update              0.704          0.726     <- recovered, nothing forgotten
```

Adaptation with zero forgetting, for the price of a coffee — the operating loop a
security-drift product runs on every policy release.

### Self-repair — the verifier loop distilled into the weights (v18)

The served system is generate -> scan -> repair -> the verifier ships the better pass.
The repair turn is *trained*: turn-1 rollouts of the current model are scanned, and its
real failures + findings become GRPO episodes (same scanner reward; no new labels, no
LLM judge — the model learns to read its own audit). Real IaC-Eval, N=120:

```
serving mode                              reward     gate
single pass (v16r)                         0.694    81.7%
two-pass, repair untrained (v16r)          0.766    85.0%
two-pass, repair trained (v18)             0.802    90.0%
```

On the never-trained holdout both systems reach 0.918 / 95.2% gates, but trained repair
converts 89% of its attempts (17/19) vs 71% untrained. Repair training also didn't cost
the generator anything (single-pass deltas within +-0.03 noise). One 49-minute, ~$3 run.

### Serving on Nebius + growing the verifier — all-open-weight, in two roles

The big rule-author model runs on Token Factory (serverless, pay-per-token). The tuned 8B is
`granite-4.1-8b`, which Token Factory does **not** host as a fine-tune base (it hosts LoRA on
Llama-3 / Qwen bases only), so it runs on a **Nebius L40S** GPU via vLLM — and self-hosts in
your VPC on the same stack when the model and infra topology can't leave the boundary. No
external/closed model anywhere.

- **Serving the 8B** (`rl_gym.iac.webdemo` — vLLM): the promoted `iac-grpo-fw1` on a **Nebius
  L40S** at ~$0.006/request amortized, on a managed Nebius container, or self-hosted in-VPC.
  Token-Factory per-token serving (`tf_policy`) is available only if you fine-tune a supported
  base (Llama-3.1-8B / Qwen2.5).
- **Growing the verifier** (`rl_gym.gym.rulegen`): new rules are *mined*, not guessed — served
  logs cross-checked against external scanners (Checkov & friends) surface the gaps, and each
  gap is a candidate rule. A big open reasoning model (`deepseek-ai/DeepSeek-V4-Pro` — per-token,
  pennies) then *drafts* the predicate, made trustworthy by two gates before a human merges it:
  1. **AST sandbox** — the drafted predicate may use only `re` + string ops; imports,
     `open`/`eval`, and dunders are rejected at compile (model code never gets capabilities).
  2. **Executable validation** — the rule must classify every pass/fail example correctly,
     or it's rejected. *This is why an open model is safe here: the tests catch its mistakes.*

  Demonstrated end to end: an open model drafted `rds_deletion_protection` (a brace-matching
  predicate that isolates each `aws_db_instance` body), it passed all four examples, and
  it's staged in `scan.py` behind `RLGYM_IAC_GENERATED_RULES=1` pending human promotion.
  A wrong rule (always-`pass`) and an unsafe one (`import os`) are both rejected by the gates.

  ```bash
  python -m rl_gym.gym.rulegen --spec rl_gym/iac/data/rulespecs/rds_deletion_protection.json \
      --model deepseek-ai/DeepSeek-V4-Pro   # draft via Token Factory, then validate
  ```

The principle, one level up from the model: **the artifact is verifiable, so the generator
is fungible** — a big LLM writes verifiers, it never gets to *be* one.

### Frontier baseline & cost (static exhibit: [docs/fable-baseline](docs/fable-baseline/README.md))

Claude Fable 5 on the first 10 dev episodes, same prompt/scanner/gates, pass@1
(`python scripts/score_fable_baseline.py` re-scores it offline):

```
condition                                reward    gate     $/request
Fable 5, blind (first contact)            0.598     70%      ~$0.17
Fable 5 + verifier rulebook (~580 tok)    1.000    100%      ~$0.16
tuned 8B fw1, pass@1                       0.738     88%      ~$0.005
tuned 8B fw1, pass@1 + self-repair (def)  0.865      —       ~$0.006
tuned 8B fw1, best-of-4 + repair (fallbk) 0.854      —       ~$0.013
```
Tuned rows are the promoted fw1 model on the real dev-60 anchor (Fable rows n=10). See the
flywheel section for the full serving 2×2 across both anchors and the cost argument for
pass@1+repair as the default.

Blind, the frontier model ties the *base* 8B — 3/10 zeroed on critical gates (wildcards in
canonical KMS key policies, unscopeable IAM actions, missing flow logs). Handed a ~580-token
distillation of the verifier, it saturates the benchmark. Both halves are the thesis: the
verifier is the learning signal (for frontier agents as much as for GRPO), and once the judge
saturates, judge *coverage* — not the generator — is the moat. Serving economics stay 12–33×
apart per request (measured tokens, June-2026 Nebius prices; break-even for a dedicated L40S
at ~230 req/day).

## Demo

```bash
# infra request in -> secure Terraform + live security scorecard, tuned vs base
python -m rl_gym.iac.demo --req "an S3 bucket for invoices, KMS-encrypted, private"

# no GPU / no model: scorecard rendering on a real IaC-Eval episode
python -m rl_gym.iac.demo --canned --episode 3
```

The scorecard prints each scanner rule (✓/✗ with severity), the security bar, both gates, and
the final reward. Try `--canned --episode 3`: the *human* reference config carries a hardcoded
secret — the `no_critical` gate zeroes it. That, in one screenshot, is why gates exist.

Best parameters are the defaults: v16 GRPO checkpoint (S3), greedy decoding, 1024-token budget,
chat template identical to training.

## Architecture

```
          Environment (protocol)                     gym layer (task-agnostic)
  ┌──────────────────────────────────┐      ┌────────────────────────────────────┐
  │ episodes(split)  prompt(ep)      │      │ sft.py    fp32 SFT, imitate oracle │
  │ parse(completion) oracle  random │ ───► │ train.py  GRPO: vLLM colocate,     │
  │ reward = Components (soft, [0,1])│      │           DAPO knobs, LoRA, s3://  │
  │        + Gates (hard, zero it)   │      │ eval.py   compare + hacking audit  │
  └──────────────────────────────────┘      │ s3io      cache + persist          │
     iac/  scan.py: 18 rules, Checkov if installed  └──────────────────────────────┘
```

One `Environment` swap retargets the whole pipeline — same trainers, eval, audit, serving.

## Data (all public, attributed)

- **Parametric generator** (`iac/tasks.py`): 8 randomized resource builders composed into 1–3
  resource stacks → 395 distinct prompts, every episode with `0 < floor < ceiling` (informative
  GRPO groups; this fixed an early flatline where 4 static templates gave zero-variance groups).
- **[IaC-Eval](https://huggingface.co/datasets/autoiac-project/iac-eval)** (CC-BY-4.0): 455
  usable human request→Terraform pairs; dev benchmark + training requests.
- **GitHub-mined** (`scripts/mine_github_tf.py`): 274 episodes from 92 repos across
  terraform-aws-modules / cloudposse / aws-ia (Apache-2.0/MIT, license-filtered via API,
  attribution per row) — 248 distinct AWS resource types. Oversized module files become
  training-only episodes: the reward needs only (request, required types), not a reference.

## Reproduce on Nebius

```bash
nebius profile activate <profile>

# Repo + prebuilt image are PUBLIC — no GitHub token needed.
# S3 defaults to the author's private bucket, so disable it for a clean third-party run:
S3_BUCKET= bash scripts/nebius_launch_iac.sh             # full pipeline (~2.5h, 1x H100), logs-only

# To persist the model + reuse a base-weights cache, point at YOUR bucket + a matching creds secret:
#   S3_BUCKET=<your-bucket> S3_CREDS_SECRET=<your-secret> bash scripts/nebius_launch_iac.sh
# (GH_PAT=... is only needed if you run a PRIVATE fork/image; USE_SECRET=1 for MysteryBox creds.)
```

Efficiency choices that made iteration cheap:
- **Prebuilt image** `ghcr.io/.../rl-gym-gpu` (torch 2.11 / vLLM 0.24 / trl 1.7 baked; built on
  x86 CI, never on an ARM laptop) — skips the multi-minute pip install every run. It builds from
  [`docker/Dockerfile`](docker/Dockerfile) (public `nvidia/cuda` base + `requirements-gpu.txt`) via
  [`.github/workflows/build-image.yml`](.github/workflows/build-image.yml). If you can't pull the
  prebuilt image, `docker build -f docker/Dockerfile -t rl-gym-gpu .` reproduces it; or the job
  script falls back to the public `docker.io/vllm/vllm-openai:v0.24.0` image + a `pip install`.
- **Self-warming S3 weight cache**: the 7B downloads from HF once, then intra-region pulls.
- **Bounded AI Jobs** that save models to S3 *before* eval — a flaky eval can't lose a run.
- vLLM colocate rollouts (~7 s/step at 1024-token completions vs infeasible HF-generate).

Best hyperparameters (v16, base ibm-granite/granite-4.1-8b): LoRA r16/α32 · SFT fp32, lr 1e-5, warmup 0.1, 2 epochs ·
GRPO lr 1e-5, β=0.04, G=4, temp 0.9, ε_high 0.28 (Clip-Higher), dapo loss, 1024-token cap.
(Note: bf16 SFT NaN-collapsed deterministically on this stack; fp32 fixed it. GRPO is fine in bf16.)

## Positioning

- **Hard gates + the reward-hacking audit are first-class**, not an afterthought: the reward
  can't be gamed by empty stubs or condition-laundered wildcards, and the audit shows *how* the
  model improved (uniq%, per-component breakdown), not just that a number moved. The
  "deceptive fix" failure mode — a repair that looks secure while preserving the vulnerability —
  is exactly what `builds_required`/`no_critical` catch (see the empty-config exploit test in
  `tests/iac_smoke_test.py`).
- **Own your model, end to end** — SFT → GRPO → audit → serve → continual update, on open
  weights and open infra, with a deterministic verifier as the reward. No closed model in the loop.
- The `Environment` interface is deliberately small and portable; the contribution is the
  **audited-reward layer** and the own-your-model pipeline on top of it.

## Limitations & next

- GRPO's edge over SFT on IaC-Eval shrank to +0.015 under the clean split (it remains +0.10 on
  the holdout); the two-pass repair system closes most of that gap (0.694 -> 0.802). Next
  training targets: difficulty-aware RAFT selection, broader failure mixes for repair.
- Scanner = 18 built-in rules + 5 drift rules (Checkov used when installed); a Checkov daemon
  for per-step RL and OPA/Rego policies (IaC-Eval ships them per-task) are the
  verifier-coverage upgrade path.
- The drift demo above is one cycle; the roadmap is the standing loop — LLM-drafted rules from
  CVE/policy feeds behind human review, a $1-2 continual update per release, promotion gated
  on the frozen anchors.

## License

**Code: [GNU AGPL-3.0](LICENSE).** © 2026 Nikita Shigarov. You can use, modify, and self-host
rl-gym freely — but any **distributed or network-served derivative must be released in full,
in source, under AGPL-3.0**. That copyleft is deliberate: it keeps the project open while
preventing anyone from building a *closed* product or hosted service on it.

**Commercial licenses** — for building a proprietary product or service on rl-gym *without* the
AGPL copyleft obligation — are available from the author. Open an issue or get in touch.

**Bundled data keeps its upstream license.** The Checkov test fixtures
(`rl_gym/iac/data/checkov_tf.jsonl`, `github_tf.jsonl`) come from
[bridgecrewio/checkov](https://github.com/bridgecrewio/checkov) (Apache-2.0, attributed per
record); [IaC-Eval](https://huggingface.co/datasets/autoiac-project/iac-eval) is under its own
dataset terms. Any AWS keys in that data (e.g. `AKIAIOSFODNN7EXAMPLE`) are the **public
AWS-documentation example credentials** used as intentional test vulnerabilities — not real
secrets.
