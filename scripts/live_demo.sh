#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# rl-gym — LIVE end-to-end story. Run it and narrate each act.
#
#   bash scripts/live_demo.sh
#
# Acts 1–2 need NOTHING (no GPU, no key, no network) — always runnable.
# Act 3 runs LIVE on Nebius Token Factory — export your key first:
#   export TOKEN_FACTORY_API_KEY=...        # your (rotated) key
# Act 4 is the payoff scorecard + links.
#
# Each act pauses so you can talk over it. Press ENTER to fire the command.
# ─────────────────────────────────────────────────────────────────────────────
set -u
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-python3}

B=$'\033[1m'; DIM=$'\033[2m'; G=$'\033[1;32m'; R=$'\033[1;31m'; Y=$'\033[33m'
M=$'\033[1;35m'; C=$'\033[1;36m'; X=$'\033[0m'

act () { printf "\n${M}════════════════════════════════════════════════════════════════════════${X}\n"; printf "${M}  %s${X}\n" "$1"; printf "${M}════════════════════════════════════════════════════════════════════════${X}\n"; }
say () { printf "${DIM}  %s${X}\n" "$1"; }
pause () { printf "\n${C}  ▶ press ENTER to run${X}"; read -r _; }

clear 2>/dev/null || true
printf "${B}rl-gym — audited RLVR for secure Terraform${X}\n"
printf "${DIM}A deterministic security scanner IS the reward. Four acts, live.${X}\n"

# ── ACT 1 ────────────────────────────────────────────────────────────────────
act "ACT 1 · The verifier is the ground truth (why gates exist)"
say "A real human-written reference config from IaC-Eval. It compiles. It looks fine."
say "Watch the scanner read it — and watch one critical finding zero the whole reward."
say "CLAIM: 'looks correct' and 'is verifiably not broken' are different things."
pause
$PY -m rl_gym.iac.demo --canned --episode 3
printf "\n${G}  ↑ 0.429 → 0.000. The human config ships password = \"password\".${X}\n"
printf "${G}    The no_critical gate is un-gameable. THIS is the reward GRPO trains against.${X}\n"

# ── ACT 2 ────────────────────────────────────────────────────────────────────
act "ACT 2 · The verifier grows itself — safely (LLM writes rules, never IS one)"
say "A big model can DRAFT new scanner rules. We make its code trustworthy with two gates:"
say "  1. AST sandbox   — only re + string ops; no import/open/eval (no capabilities)"
say "  2. executable     — must classify every pass/fail example, or it's rejected"
say "Three candidate rules go in: one correct, one lazy, one malicious. Watch the gates."
pause
SP="$(mktemp -d)"; SPEC=rl_gym/iac/data/rulespecs/rds_deletion_protection.json

cat > "$SP/good.py" <<'PY'
def rule(hcl):
    if "aws_db_instance" not in hcl:
        return "na"
    found, idx = False, 0
    while True:
        m = re.search(r'resource\s+"aws_db_instance"', hcl[idx:])
        if not m:
            break
        found = True
        brace = hcl.find("{", idx + m.start())
        depth, i = 0, brace
        while i < len(hcl):
            if hcl[i] == "{": depth += 1
            elif hcl[i] == "}":
                depth -= 1
                if depth == 0: break
            i += 1
        if not re.search(r'deletion_protection\s*=\s*true', hcl[brace:i]):
            return "fail"
        idx = i + 1
    return "pass" if found else "na"
PY
cat > "$SP/lazy.py"    <<'PY'
def rule(hcl):
    return "pass"   # always says it's fine — the classic reward hack
PY
cat > "$SP/malicious.py" <<'PY'
def rule(hcl):
    import os
    open("/etc/passwd").read()   # tries to touch the filesystem
    return "fail"
PY

for c in good lazy malicious; do
  printf "\n${B}  ── candidate: %s ──${X}\n" "$c"
  $PY -m rl_gym.gym.rulegen --spec "$SPEC" --candidate "$SP/$c.py" 2>&1 | grep -E "example ->|VERDICT|REJECTED"
done
rm -rf "$SP"
printf "\n${G}  ↑ Correct rule ACCEPTS. The lazy always-pass is caught by the tests.${X}\n"
printf "${G}    The malicious import is caught at compile. A weak model is SAFE here.${X}\n"

# ── ACT 3 ────────────────────────────────────────────────────────────────────
act "ACT 3 · The whole loop, LIVE on Nebius Token Factory (open weights, no GPU)"
if [ -z "${TOKEN_FACTORY_API_KEY:-}" ]; then
  printf "${Y}  TOKEN_FACTORY_API_KEY not set — skipping the live act.${X}\n"
  say "To run it live:  export TOKEN_FACTORY_API_KEY=...  then re-run this script."
  say "It runs: a big open model writes Terraform → verifier judges → self-repair fixes →"
  say "the big model AUTHORS a new rule (gated) → distillation punchline. ~30s, a few cents."
else
  say "A big open model (DeepSeek-V4-Pro) writes Terraform, the verifier judges it, the model repairs"
  say "its own findings, then AUTHORS a new gated rule — all serverless, live."
  pause
  $PY scripts/demo_e2e.py --model deepseek-ai/DeepSeek-V4-Pro \
      --json "$(mktemp -d)/run.json"
fi

# ── ACT 4 ────────────────────────────────────────────────────────────────────
act "ACT 4 · The payoff — a tuned 8B beats a rented giant where it counts"
cat <<TXT
  ${B}On real IaC-Eval (120 human requests, clean disjoint split):${X}
    base 8B            0.629  76.7% gate
    + GRPO (RLVR)      0.694  81.7% gate      1.5x the human references on security
    + self-repair      0.802  90.0% gate

  ${B}Frontier vs specialist (measured \$/request):${X}
    frontier, blind    0.598  70%   ~\$0.17     ← ties the BASE 8B; 3/10 zeroed on criticals
    tuned 8B + repair  0.865   —    ~\$0.006    ← 28x cheaper, reliably secure

  ${B}The flywheel (served traffic → next model, a PROGRAM decides it ships):${X}
    real dev  0.697 → 0.738  (+0.041)
    holdout   0.859 → 0.906  (+0.047)   collapse alarm: none → ${G}PROMOTE${X}

  ${B}And the honesty that makes it credible:${X}
    a +0.04 data leak in an earlier number — caught, quantified, corrected.
TXT
printf "\n${C}  Hosted console:${X} https://claude.ai/code/artifact/a970de7f-1bc3-4939-9027-cf435b706dee\n"
printf "${C}  Live eval tables from S3 (needs a Nebius profile):${X} EVAL_ONLY=1 bash scripts/nebius_launch_iac.sh\n"
printf "\n${G}${B}  The division of labor, shown live: big model WRITES rules · verifier JUDGES · small model SERVES.${X}\n\n"
