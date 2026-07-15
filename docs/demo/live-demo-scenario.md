# Live demo scenario — the full flow across all interfaces

A single story a presenter can execute end to end, touching every part of the system.
The framing to open with:

> *"You're a platform engineer. Your job is to ship infrastructure that's not just correct,
> but verifiably secure — and to keep it secure as the rules change. Here's a system that
> does that with a small open model, a deterministic verifier, and a loop that improves itself."*

---

## The three interfaces you'll use

| # | Interface | Brings up | Shows | Ready now? |
|---|---|---|---|---|
| **A** | **Web UI** (browser) | a Nebius GPU job | generation · verifier scorecard · self-repair · policy-drift · frontier cost column | needs launch (below) |
| **B** | **Hosted console** (a web page) | already live | the end-to-end Token Factory arc: big model writes → verifier judges → repair → **authors a gated rule** | ✅ now |
| **C** | **CLI** (`scripts/live_demo.sh`) | your laptop | the same, "under the hood" — gate zeroing + rule-authoring gates, no GPU | ✅ now |

**Bring up Interface A (do this ~15 min before you present — model load takes ~11 min):**

```bash
WEBDEMO=1 WEBDEMO_DRIFT=1 GH_PAT=ghp_...  bash scripts/nebius_launch_iac.sh
# watch the job logs — a public https URL prints (Cloudflare quick tunnel). Open it.
# WEBDEMO_DRIFT=1 adds the drift-updated engine and enables the policy-update toggle.
# The job bills until its timeout (~L40S $1.55/h) — kill it when you're done.
```

**Interface B is live now:** https://claude.ai/code/artifact/a970de7f-1bc3-4939-9027-cf435b706dee
**Interface C is on your laptop now:** `bash scripts/live_demo.sh`

---

## The scenario — 6 beats

### Beat 1 — Generation + the verifier is the judge  · *Interface A*
- **Do:** click the **`clean 1.0`** showcase chip → **Generate + scan**.
- **See:** the tuned 8B's card — a reward near the top, every scanner rule with a green ✓ or red
  ✗ and its severity, both gates as green `pass` pills, the security bar full, the cost (~$0.006)
  and latency, and (expand *Terraform*) the config with **hardening lines highlighted green**.
- **Say:** *"An 8B open model, GRPO-tuned against a security scanner. The scanner isn't a
  suggestion — it's the reward the model was trained on, and it judges every answer live. This is
  a verifiably secure config, selected by the verifier."*

### Beat 2 — Self-repair: the model reads its own audit  · *Interface A*
- **Do:** click the **`self-repair`** showcase chip → **Generate + scan**.
- **See:** two cards side by side — **① first attempt** (misses VPC flow logs, reward < 1.0) →
  **② after self-repair** (1.00). The added lines carry a bold green ✓ in the gutter, and the
  status line reads `self-repair: 0.xx → 1.00 ▲ the model read its own findings and fixed them`.
- **Say:** *"When the first pass isn't perfect, it doesn't reroll blindly — it reads the scanner's
  findings and rewrites the specific gap. And that repair turn is trained into the weights: on the
  benchmark it converts 89% of repair attempts, versus 71% for the same loop untrained."*

### Beat 3 — The standard changes; the system adapts  · *Interface A*
- **Do:** click the **`new rule → instant adapt`** chip (or tick the **policy-update** toggle) →
  **Generate + scan**.
- **See:** the scorecard now includes rules badged **NEW**; the before/after cards show the model
  failing the new rule on attempt ① and satisfying it on ② via self-repair. Status:
  `policy update · NEW rule ... → self-repair adapted 0.xx → 1.00 ▲`.
- **Say:** *"Security standards drift. A new rule takes effect in the scanner — the model was never
  rewarded on it — and it adapts in-context immediately. Separately, a one-hour, ~$1.30 continual
  update bakes that into the weights with zero forgetting. Adaptation is the operating loop, not a
  re-training project."*

### Beat 4 — A frontier model, blind, at 28× the cost  · *Interface A*
- **Do:** run any of the four **preset showcase** requests (they have a recorded frontier answer),
  and look at the frontier column next to the tuned one.
- **See:** the frontier model's card scored by the *same* scanner — often tying the base 8B and
  zeroing on a critical — with a cost around **$0.17** versus the tuned 8B's **~$0.006**.
- **Say:** *"This isn't 'small model good, big model bad.' Blind, the frontier model ties the
  untuned 8B and costs ~28× more. Hand it the verifier's rulebook and it saturates — which is the
  whole point: the verifier is the signal. The specialist just makes it cheap and reliable."*

### Beat 5 — Growing the verifier itself, with a big model  · *Interface B (then C)*
- **Do:** open the **hosted console** and walk its four acts; then, for the skeptic, run it *live*
  on your laptop: `TOKEN_FACTORY_API_KEY=... python scripts/demo_e2e.py --episode 3`.
- **See:** a big open model (DeepSeek-V4-Pro) writes Terraform → the verifier judges it → the model repairs it →
  **the big model authors a brand-new scanner rule**, which passes an AST sandbox and every
  executable test and goes live. (In Interface C you can also show the gates *rejecting* a lazy
  always-`pass` rule and a malicious `import os` rule — `scripts/live_demo.sh` Act 2.)
- **Say:** *"The other half of the system: the verifier isn't fixed. A big open model drafts new
  rules, but its code is guilty until proven innocent — sandboxed to regex-and-strings, and it must
  pass every example or it's rejected. A big LLM writes verifiers; it never gets to be one."*

### Beat 6 — The flywheel: this traffic becomes the next model  · *callback to A*
- **Do:** no new click — point back at the web UI.
- **See / Say:** *"Every request you just watched was logged to S3 as a training candidate — that's
  the flywheel's intake. Pooled traffic trains a continual update, and an **executable gate**
  compares it to the current model on frozen anchors and prints PROMOTE or BLOCK — no human in the
  decision. Its first real cycle promoted a model that scored +0.041 on real requests and +0.047 on
  holdout, for about $4. Serving traffic literally produces the next model, and a program decides
  it ships."*

**Close:** *"Big open model writes the rules. A deterministic verifier judges everything. A small
tuned model serves it cheap and reliably. And a gated loop turns usage back into a better model.
All open-weight, all serverless on Nebius, nothing leaves the boundary."*

---

## Coverage map — every part of the system is shown

| System part | Where in the scenario |
|---|---|
| Secure generation (tuned 8B) | Beat 1 (A) |
| Verifiable reward + hard gates | Beat 1 (A), Beat 5 live (C) |
| Trained self-repair | Beat 2 (A) |
| Continual learning / drift | Beat 3 (A) |
| Frontier baseline + cost economics | Beat 4 (A) |
| Big-model rule authoring + safety gates | Beat 5 (B, C) |
| Serverless / open-weight / Token Factory | Beat 5 (B) |
| Serving → retrain flywheel, gated | Beat 6 (callback) |

## If the GPU job isn't up (no Interface A)

The story still lands on **B + C alone** — you lose the interactive scorecard but keep the whole
arc: run `scripts/live_demo.sh` (gate zeroing → rule-authoring gates → live Token Factory loop →
payoff scorecard) and open the hosted console. Beats 1–4 become "here's what the measured results
show" using the Act 4 scorecard instead of live clicks.

## Timing & practical notes
- Full flow with Interface A live: **~5–6 minutes**. B + C only: **~3 minutes**.
- Launch the web job **~15 min early** (model load) and **kill it right after** (it bills per hour).
- Use a **rotated** `GH_PAT` and `TOKEN_FACTORY_API_KEY` — never the ones pasted in chat.
- The showcase chips are curated to land cleanly; free-form requests work too but are less
  predictable live (that's the honest version — mention it if you type one).
