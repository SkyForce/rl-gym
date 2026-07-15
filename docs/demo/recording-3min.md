# 3-minute demo recording — one browser tab, real models

The whole system in a single take on the **web UI**, served on a **Nebius L40S** job:
the **real granite-4.1-8b tune** generates + self-repairs, and (same tab) a **big open
model — DeepSeek-V4-Pro — authors a new scanner rule** live, gated before it ships.

> One tab shows both loops. No interface switching, no splicing. Every model on screen
> is the real one: the tuned 8B on the L40S, DeepSeek-V4-Pro on Token Factory.

---

## Pre-flight (do ~15 min before you hit record)

The web-demo job clones the **public** repo and launches `webdemo.py` from it, so the
grow-the-verifier panel needs the two changes on `master` (unified `webdemo.py` + the
`TOKEN_FACTORY_API_KEY` passthrough) already pushed.

```bash
# rotated keys only — never the ones pasted in chat
export TOKEN_FACTORY_API_KEY=...          # enables the "grow the verifier" panel (DeepSeek-V4-Pro)
export GH_PAT=ghp_...                      # only if the image/repo is private; public repo needs none

WEBDEMO=1 WEBDEMO_DRIFT=1 \
  bash scripts/nebius_launch_iac.sh
# ~11-min model load. Watch the job logs for:
#   [webdemo] grow-the-verifier panel: ON (deepseek-ai/DeepSeek-V4-Pro)
#   ### opening public tunnel (IaC)  ->  https://<random>.trycloudflare.com
# Open that URL. The job bills until --timeout (~L40S $1.55/h) — cancel it right after.
```

**Sanity check before recording (30s):**
- Header reads "rl-gym — secure Terraform, live".
- The four grey chips + three coloured showcase chips (`clean 1.0`, `self-repair`,
  `new rule → instant adapt`) are present.
- The **policy-update** toggle is visible (that's `WEBDEMO_DRIFT=1` working).
- Scroll down: the **"grow the verifier — mine the gaps"** panel is present, its model reads
  **DeepSeek-V4-Pro** (Token Factory key working), and the **gap card** shows a Checkov ID +
  the mined config for the selected spec.
- Browser at ~110% zoom, window clean, cache warm (click each showcase chip once so the
  first recorded click is instant).

---

## The take — 6 shots, ~180s

Numbers below are the committed ones (README / paper). Say them; don't read them off.

### 0:00–0:18 · Hook  (no click)
- **On screen:** the page header + sub-line.
- **Say:** *"Infrastructure that's not just correct, but verifiably secure — from an 8-billion-parameter
  open model. A deterministic security scanner is the reward it was trained on, and it judges every
  answer live. Here's the whole system in one tab."*

### 0:18–0:48 · Beat 1 — the verifier is the judge
- **Do:** click **`clean 1.0`** → **Generate + scan**.
- **On screen:** the tuned 8B card — reward near 1.0, each scanner rule green ✓ / red ✗ with
  severity, both gates green `pass`, cost **~$0.006**, latency; expand **Terraform** → hardening
  lines highlighted green.
- **Say:** *"The tuned 8B, running on a Nebius L40S. The scanner isn't a linter suggestion — it's
  the reward this model was trained against, and it can't be sweet-talked. A verifiably secure
  config, selected by the verifier, for about six-tenths of a cent."*

### 0:48–1:18 · Beat 2 — self-repair (reads its own audit)
- **Do:** click **`self-repair`** → **Generate + scan**.
- **On screen:** two cards — **① first attempt** (misses VPC flow logs, reward < 1.0) →
  **② after self-repair** (1.00); added lines carry a bold green ✓; status: `self-repair: 0.xx → 1.00 ▲`.
- **Say:** *"When the first pass isn't perfect it doesn't reroll blindly — it reads the scanner's
  findings and rewrites the exact gap. And that repair turn is trained into the weights: it converts
  89% of repair attempts, versus 71% for the same loop untrained."*

### 1:18–1:45 · Beat 3 — the standard drifts; the model adapts
- **Do:** click **`new rule → instant adapt`** (or tick **policy-update**) → **Generate + scan**.
- **On screen:** rules badged **NEW**; before/after cards fail the new rule on ① and satisfy it on ②.
- **Say:** *"Security standards move. A new rule takes effect in the scanner — the model was never
  rewarded on it — and it adapts in-context immediately. A one-hour, ~$1.30 continual update then
  bakes it into the weights, with zero forgetting. Adaptation is the operating loop, not a re-training project."*

### 1:45–2:40 · Beat 4 — grow the verifier: mine the gap, the big model drafts it ← the unified payoff
- **Do (discovery):** scroll to **"grow the verifier — mine the gaps"** → select
  **`rds_deletion_protection`**. Expand **"the mined gap"**.
- **On screen:** the gap card — *"blind spot from traffic × Checkov — our 18-rule scanner **passed**
  a config Checkov **CKV_AWS_293** flagged"* — and the config that slips past (a DB instance, encrypted
  but with no deletion protection).
- **Say (discovery):** *"New rules aren't hand-written. The miner runs the model's own served traffic
  through Checkov — a thousand external policies — and finds configs our scanner passed that Checkov
  failed. Those disagreements are the blind spots. Here's one: a database our scanner OK'd, missing
  deletion protection."*
- **Do (close the gap):** click **Draft the rule with the big model** (~15s live — talk over it).
- **On screen:** **DeepSeek-V4-Pro** drafts a predicate → **AST sandbox ✓** → **executable tests (4) ✓**
  → **ACCEPT — rule is now LIVE**.
- **Then:** scroll up, click the **`billing RDS`** chip → **Generate + scan** → the brand-new rule now
  appears in the scorecard and self-repair satisfies it.
- **Say (close):** *"The big model drafts the predicate to close that gap — but its code is guilty until
  proven innocent: sandboxed to regex and strings, and it must classify every example or it's rejected.
  It goes live, and the very next generation is judged under it. A big model writes verifiers; it never
  gets to *be* one."*

### 2:35–3:00 · Close — the division of labor + the flywheel
- **On screen:** stay on the last scorecard (cost visible).
- **Say:** *"So: a big open model writes the rules, a deterministic verifier judges everything, and a
  small tuned model serves it — reliably, at ~$0.006 versus ~$0.17 for a blind frontier model, 28×
  cheaper. And every request you just saw is logged as a training candidate — a gated flywheel turns
  that traffic into the next model, +0.04 on real requests, and a program, not a person, clears it to
  ship. All open-weight, all serverless on Nebius, nothing leaves the boundary."*

---

## Timing & trims
- Target **3:00**. If long, cut **Beat 3** (drift) first — it's the most redundant with Beat 2's
  before/after. Beats 1, 2, 4 are the spine (generate → repair → grow the verifier).
- The only live wait is Beat 4's ~15s draft. Everything else is instant (cached showcase chips).

## Fallbacks
- **Panel missing / model not DeepSeek-V4-Pro** → `TOKEN_FACTORY_API_KEY` didn't reach the job.
  Confirm the launcher printed the passthrough and the log line `grow-the-verifier panel: ON (...)`.
- **No policy-update toggle** → relaunch with `WEBDEMO_DRIFT=1`.
- **GPU job won't come up in time** → record Beats 1–4 as the terminal end-to-end instead:
  `TOKEN_FACTORY_API_KEY=... bash scripts/live_demo.sh` (real DeepSeek-V4-Pro; the tuned 8B is the
  Act 4 scorecard rather than a live card).

## Numbers cheat-sheet (all committed)
| Claim | Value |
|---|---|
| Tuned 8B + repair (real IaC-Eval) | **0.865** |
| Base 8B → +GRPO | 0.629 / 76.7% → **0.694 / 81.7%** (≈1.5× human refs on security) |
| Repair conversion (trained vs not) | **89% vs 71%** |
| Cost: tuned vs blind frontier | **~$0.006 vs ~$0.17 (28×)** |
| Continual weight update | **~$1.30**, zero forgetting |
| Flywheel promotion | real +0.041 · holdout +0.047 → **PROMOTE** (a program decides) |
