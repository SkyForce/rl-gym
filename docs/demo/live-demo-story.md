# Live demo — the story (talk-track)

Run it:

```bash
bash scripts/live_demo.sh
```

Acts 1–2 need **nothing** — no GPU, no key, no network. They run instantly on any laptop.
Act 3 runs **live on Nebius Token Factory**; export your key first (Acts 1, 2, 4 run without it):

```bash
export TOKEN_FACTORY_API_KEY=...   # your rotated key
bash scripts/live_demo.sh
```

Each act pauses on `▶ press ENTER` so you can talk over it, then fires the command.

---

## The arc (4 acts, ~2 minutes without the live one, ~3 with)

### ACT 1 — The verifier is the ground truth
**What runs:** the scanner scores a real *human-written* IaC-Eval reference config.
**The wow:** it drops `password = "password"` → the `no_critical` gate **zeroes 0.429 → 0.000**.
**Say:** "This is a human 'correct' answer. It compiles, it looks fine, and it's a critical
security failure. The scanner is a deterministic program — this exact score is the reward the
model is trained against. You can't sweet-talk it."

### ACT 2 — The verifier grows itself, safely
**What runs:** three candidate rules drafted for a new policy go through the authoring gates.
**The wow:** correct rule **ACCEPTs**; the lazy always-`pass` reward-hack is caught by the
**executable tests**; the malicious `import os` is caught by the **AST sandbox** at compile.
**Say:** "A big model can *write* new rules for us — but its code is guilty until proven innocent.
Two gates: it can only use regex and strings, and it must pass every example. That's why a cheap
open model is safe as the author — it never gets to *be* the judge."

### ACT 3 — The whole loop, live on Token Factory
**What runs:** `demo_e2e.py` against Qwen3-235B — a 235B open model writes Terraform, the verifier
judges it, the model repairs its own findings, then **authors a new gated rule** live.
**The wow:** it's all real, all serverless, all open-weight, and it costs a few cents.
**Say:** "Big model writes and repairs, deterministic verifier judges, new rule gets vetted and
goes live — no GPU, nothing closed, nothing leaves the boundary."
*(No key? The script skips this and tells you how to enable it.)*

### ACT 4 — The payoff
**What runs:** a static scorecard of the measured results + the links.
**The wow, in three numbers:**
- tuned 8B + repair **0.865** at **~$0.006** vs a blind frontier model **0.598** at **~$0.17** (28×).
- the flywheel promoted a better model from served traffic — **a program**, not a person, cleared it.
- a **+0.04 data leak** in an earlier number, caught and corrected — the audit working on itself.

---

## If a skeptic pushes in the room

- *"You compared a tuned model to a blind frontier one — unfair."* → Correct, and that's half the
  thesis: hand the frontier model the verifier's ~580-token rulebook and it saturates to 1.000.
  **The verifier is the signal.** The specialist just makes it cheap and reliable.
- *"Is the scanner the real bottleneck?"* → Yes — once the judge saturates, judge *coverage* is the
  moat, which is exactly why Act 2 (growing the verifier with a big model) exists.
- *"Numbers real?"* → Every figure traces to a table in the README; the leak correction is in there
  on purpose.

## One-liners (if you'd rather not run the full script)

```bash
python -m rl_gym.iac.demo --canned --episode 3          # Act 1 — the gate zeroing
python -m rl_gym.gym.rulegen --spec rl_gym/iac/data/rulespecs/rds_deletion_protection.json \
    --candidate <your_rule.py>                           # Act 2 — the authoring gates
TOKEN_FACTORY_API_KEY=... python scripts/demo_e2e.py     # Act 3 — the live loop
```
