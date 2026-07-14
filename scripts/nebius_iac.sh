#!/usr/bin/env bash
# Nebius AI Job — IaC vertical kill-test: GRPO a 7B model on secure-Terraform
# generation (scanner = verifiable reward), then compare + reward-hacking audit,
# save to S3. No SFT — GRPO directly from the base instruct model.
#
# Env: MODEL (Qwen2.5-7B-Instruct)  OUT (/workspace/out)  GRPO_STEPS (120)  NUM_GEN (4)
#      S3_BUCKET + AWS_ACCESS_KEY_ID/SECRET (+ S3_PREFIX, default rl-gym-iac) -> S3 save
set -euo pipefail
MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct}"
OUT="${OUT:-/workspace/out}"
GRPO_STEPS="${GRPO_STEPS:-120}"
NUM_GEN="${NUM_GEN:-4}"
mkdir -p "$OUT"

# Deps: the prebuilt GPU image (ghcr.io/<owner>/rl-gym-gpu, see docker/Dockerfile)
# already has the whole torch+vLLM+trl stack baked in — skip the multi-minute apt+pip.
# On a bare image, fall back to installing them (so this script works either way).
if python -c "import trl, vllm, peft, datasets" 2>/dev/null; then
  echo "### deps present (prebuilt image) — skipping apt/pip"
  # tokenizer-conversion deps (v10: slow->fast conversion failed without them);
  # tiny install until the rebuilt image includes them
  python -c "import sentencepiece, tiktoken" 2>/dev/null || pip install -q sentencepiece tiktoken
else
  echo "### bare image — installing build-essential + modern trl+vLLM (colocate) stack"
  # vLLM/torch.compile (triton/inductor) needs a C compiler; a *modern* trl is needed for
  # in-process colocate vLLM (0.16.1 was server-only). Let pip resolve a mutually-compatible
  # trl/transformers/vllm set (co-developed) — the Docker image freezes this same resolution.
  apt-get update -qq && apt-get install -y -qq build-essential >/dev/null 2>&1 || true
  pip install -q -U trl transformers vllm peft accelerate datasets boto3 huggingface_hub
fi
# Reward uses the built-in scanner (fast). Real Checkov (1000+ policies) is a
# subprocess-per-generation — too slow for per-step RL; wire it as a daemon later.

# --- base-weights cache: download the 7B from HF ONCE, store in S3, then every
# later run pulls it intra-region (skips the ~15GB HF download + rate limits).
# Self-warming: first run seeds the cache; train + eval both read the s3:// path.
# Resilient: if the bucket is full or S3 is unreachable, fall back to the HF id —
# a caching miss must never break the run (it just loses the speedup).
MODEL_ARG="$MODEL"
if [ -n "${S3_BUCKET:-}" ] && [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then
  echo "### S3 bucket headroom (watch vs the size limit; base cache adds ~15GB):"
  python -m rl_gym.gym.s3io usage || true
  BASE_PREFIX="${BASE_PREFIX:-base/$(basename "$MODEL")}"
  if python -m rl_gym.gym.s3io exists --prefix "$BASE_PREFIX"; then
    echo "### base cached at s3://$S3_BUCKET/$BASE_PREFIX — pulling intra-region (skips HF)"
    MODEL_ARG="s3://$S3_BUCKET/$BASE_PREFIX"
  elif python - "$MODEL" "$BASE_PREFIX" <<'PY'
import sys
from huggingface_hub import snapshot_download
from rl_gym.gym.s3io import upload_dir
model, prefix = sys.argv[1], sys.argv[2]
p = snapshot_download(model, ignore_patterns=["*.pth", "*.pt", "*.bin", "original/*"])  # safetensors only
upload_dir(p, prefix)
PY
  then
    echo "### base downloaded from HF and cached to S3 for next run"
    MODEL_ARG="s3://$S3_BUCKET/$BASE_PREFIX"
  else
    echo "### WARN: base cache warm failed (bucket full / perms?) — using HF model directly"
    MODEL_ARG="$MODEL"
  fi
fi

# CLEAN=1: S3 bucket cleanup — dry-run inventory unless CONFIRM=1. No GPU work; needs the
# S3 creds secret. Keeps the models needed for the demo + article (scripts/s3_cleanup.py).
if [ -n "${CLEAN:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  python -c "import boto3" 2>/dev/null || pip install -q boto3
  python scripts/s3_cleanup.py
  exit 0
fi

# MINE=1: self-improving loop DISCOVERY — generate the model's own traffic, run Checkov,
# rank the coverage gaps (Checkov fails a config our scanner passes). Gap corpus -> S3.
if [ -n "${MINE:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  python -c "import checkov" 2>/dev/null || pip install -q checkov
  MINE_MODEL="${MINE_MODEL:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-fw1}"
  python scripts/gap_mine.py --model "$MINE_MODEL" --n "${EVAL_N:-120}" --out "${MINE_OUT:-gapmine}"
  exit 0
fi

# WEBDEMO=1: serve the LIVE browser demo (FastAPI + vLLM, base vs tuned) with a public
# URL via a Cloudflare quick tunnel (no account; URL prints in the job logs). The job
# stays up until its --timeout (or `nebius ai job cancel`) — that is the billing boundary.
if [ -n "${WEBDEMO:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  VER="${VERSION:+-$VERSION}"
  TUNED="s3://$S3_BUCKET/$S3_PREFIX/iac-grpo$VER"
  # WEBDEMO_DRIFT=1: third engine (the continually-updated checkpoint) + the page's
  # "policy update" toggle. Three fp8 engines on a 48GB L40S need the smaller frac.
  DRIFT_ARGS=""
  if [ -n "${WEBDEMO_DRIFT:-}" ]; then
    DRIFT_ARGS="--drift s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-drift$VER"
    WEBDEMO_GPU_FRAC="${WEBDEMO_GPU_FRAC:-0.27}"
  fi
  echo "### fetching cloudflared (public quick tunnel) — via python (the image has NO curl; v1/v3 died on that)"
  CF=/workspace/cloudflared
  python - "$CF" <<'PY' && chmod +x "$CF" && echo "### cloudflared ok" || CF=""
import sys, urllib.request
url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
try:
    urllib.request.urlretrieve(url, sys.argv[1]); print("downloaded", url)
except Exception as e:
    print("### WARN: cloudflared download failed:", type(e).__name__, e); raise SystemExit(1)
PY
  echo "### starting live web demo (FastAPI :8000)"
  # Product-focused demo: tuned (+ drift) columns only — base/Fable comparisons live in
  # the presentation. WEBDEMO_BASE=1 opts the base column back in (3 engines -> 0.27).
  BASE_ARGS=""
  if [ -n "${WEBDEMO_BASE:-}" ]; then
    BASE_ARGS="--base $MODEL_ARG"
    [ -n "${WEBDEMO_DRIFT:-}" ] && WEBDEMO_GPU_FRAC="${WEBDEMO_GPU_FRAC:-0.27}"
  fi
  python -m rl_gym.iac.webdemo --tuned "$TUNED" $BASE_ARGS --port 8000 \
    --gpu_frac "${WEBDEMO_GPU_FRAC:-0.42}" $DRIFT_ARGS &
  APP=$!
  # wait for the server (python probe — no curl in image)
  python - <<'PY'
import time, urllib.request
for _ in range(180):
    try:
        urllib.request.urlopen("http://localhost:8000", timeout=3); print("server is up"); break
    except Exception:
        time.sleep(5)
PY
  echo "### opening public tunnel (IaC)"
  if [ -n "$CF" ]; then
    "$CF" tunnel --no-autoupdate --url http://localhost:8000 &
  else
    # fallback: keyless reverse-SSH tunnel (localhost.run prints a public https URL)
    apt-get install -y -qq openssh-client >/dev/null 2>&1 || true
    ( while :; do ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
        -R 80:localhost:8000 nokey@localhost.run 2>&1 | grep --line-buffered -E "https://|tunneled"; sleep 5; done ) &
  fi
  wait $APP
  exit 0
fi

# BAKEOFF=1: no training — score candidate BASE models head-to-head on the real
# IaC-Eval dev split (which base should the next full train start from?). Qwen3 gets
# " /no_think" so hybrid thinking doesn't eat the completion budget.
if [ -n "${BAKEOFF:-}" ]; then
  for M in "Qwen/Qwen2.5-7B-Instruct|" "Qwen/Qwen2.5-Coder-7B-Instruct|" \
           "Qwen/Qwen3-8B| /no_think" "ibm-granite/granite-4.1-8b|"; do
    model="${M%%|*}"; suf="${M#*|}"
    echo "================ BAKEOFF: $model (suffix='${suf}') ================"
    RLGYM_PROMPT_SUFFIX="$suf" python -m rl_gym.gym.eval compare --env iac --data_dir real \
      --n_episodes "${N_EPISODES:-30}" --base "$model" || true
  done
  exit 0
fi

# DEMO=1: no training — run the showcase demo (tuned-vs-base scorecards) against the
# S3-saved v12 model on real IaC-Eval episodes + one custom request; logs are the show.
if [ -n "${DEMO:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  TUNED="s3://$S3_BUCKET/$S3_PREFIX/iac-grpo"
  echo "================ DEMO: real IaC-Eval requests ================"
  python -m rl_gym.iac.demo --episodes "${DEMO_EPISODES:-1,3,7}" \
    --base "$MODEL_ARG" --tuned "$TUNED"
  echo "================ DEMO: custom request ================"
  python -m rl_gym.iac.demo --req "an S3 bucket for customer invoices with KMS encryption, versioning, and no public access" \
    --required aws_s3_bucket --base "$MODEL_ARG" --tuned "$TUNED"
  exit 0
fi

# Shared drift stage (continual-learning demo): PRE_DRIFT_SRC may be a local dir OR an
# s3:// prefix — so the demo can run inside a training job or a standalone eval job.
run_drift() {
  local PRE_DRIFT_SRC="$1"
  local DN="${DRIFT_EVAL_N:-60}"
  export RLGYM_IAC_DRIFT_RULES=1
  { echo "================ DRIFT 1/3: v17 under the NEW scanner (expected drop) ================"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --grpo "$PRE_DRIFT_SRC" --grpo_label "GRPO v17 (old rules)" || true; } 2>&1 | tee -a "$RES"; save_res
  echo "### DRIFT continual update: ${DRIFT_STEPS:-50} steps from the v17 checkpoint"
  python -m rl_gym.gym.train --env iac --data_dir real --model "$PRE_DRIFT_SRC" --lora \
    --num_generations "$NUM_GEN" --max_steps "${DRIFT_STEPS:-50}" --lr 1e-5 \
    --beta "${BETA:-0.04}" --vllm_gpu_mem "${VLLM_GPU_MEM:-0.3}" \
    --epsilon_high 0.28 --loss_type dapo ${DYNSAMP:+--dynamic_sampling} \
    --max_completion_len "${MAX_COMPLETION_LEN:-1024}" --max_prompt_len 512 \
    --output_dir "$OUT/iac-grpo-drift" \
    || { echo "### WARN: drift update failed — demo tables skipped"; unset RLGYM_IAC_DRIFT_RULES; return 0; }
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$OUT/iac-grpo-drift" --prefix "$S3_PREFIX/iac-grpo-drift$VER" || true
  { echo "================ DRIFT 2/3: updated model under the NEW scanner (recovery) ================"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --sft "$PRE_DRIFT_SRC" --sft_label "GRPO v17 (pre-drift)" \
      --grpo "$OUT/iac-grpo-drift" --grpo_label "v17.1 (continual)" || true; } 2>&1 | tee -a "$RES"; save_res
  unset RLGYM_IAC_DRIFT_RULES
  { echo "================ DRIFT 3/3: updated model under the OLD scanner (no forgetting) ================"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --sft "$PRE_DRIFT_SRC" --sft_label "GRPO v17 (pre-drift)" \
      --grpo "$OUT/iac-grpo-drift" --grpo_label "v17.1 (continual)" || true; } 2>&1 | tee -a "$RES"; save_res
}

# EVAL_ONLY=1: skip training entirely and evaluate the models already saved in S3
# (v12: a full pipeline + 3 tables of 1024-token evals overran the 2h job timeout;
# save-before-eval means the models survive — this mode re-runs just the tables).
# Honors VERSION (suffixed model prefixes), EVAL_VLLM (batched fast eval), DRIFT=1.
if [ -n "${EVAL_ONLY:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  VER="${VERSION:+-$VERSION}"
  SFT_S3="s3://$S3_BUCKET/$S3_PREFIX/iac-sft$VER"
  GRPO_S3="s3://$S3_BUCKET/$S3_PREFIX/iac-grpo$VER"
  N_EP="${EVAL_N:-${N_EPISODES:-40}}"; AUDIT_N="${AUDIT_N:-$N_EP}"
  export RLGYM_IAC_TRAIN_N="${TRAIN_N:-600}"
  [ "${EVAL_VLLM:-1}" = "1" ] && export RLGYM_EVAL_VLLM=1
  # Results go to S3, not just stdout — Nebius log retention is shorter than a long
  # eval (the first N=120 run COMPLETED but its tables expired unread). Upload after
  # EVERY table so even a timeout preserves whatever finished.
  RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/results$VER-$(date +%Y%m%d-%H%M).txt"
  save_res() { python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true; }
  { echo "================ RESULTS (base -> SFT -> GRPO -> oracle) ================"
    python -m rl_gym.gym.eval compare --env iac --n_episodes "$N_EP" --base "$MODEL_ARG" --sft "$SFT_S3" --grpo "$GRPO_S3" || true; } 2>&1 | tee -a "$RES"; save_res
  { echo "================ REWARD-HACKING AUDIT ================"
    python -m rl_gym.gym.eval audit --env iac --n_episodes "$AUDIT_N" --base "$MODEL_ARG" --sft "$SFT_S3" --grpo "$GRPO_S3" || true; } 2>&1 | tee -a "$RES"; save_res
  { echo "================ REAL-WORLD EVAL (IaC-Eval) ================"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$N_EP" --base "$MODEL_ARG" --sft "$SFT_S3" --grpo "$GRPO_S3" || true; } 2>&1 | tee -a "$RES"; save_res
  { echo "================ HOLDOUT EVAL (mined, never-trained source) ================"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --split holdout \
      --n_episodes "$N_EP" --base "$MODEL_ARG" --sft "$SFT_S3" --grpo "$GRPO_S3" || true; } 2>&1 | tee -a "$RES"; save_res
  [ -n "${DRIFT:-}" ] && run_drift "$GRPO_S3"
  echo "### results persisted to s3://$S3_BUCKET/$S3_PREFIX/evalres/"
  exit 0
fi

# DISCOVER=1: the self-improving loop's PAYOFF. Enable the gap-mined discovered rule
# (kms_key_policy, from fw1 traffic x Checkov, DeepSeek-drafted), measure fw1's drop (the
# blind spot), run ONE continual update, and re-eval (recovery) — plus a no-forgetting check.
if [ -n "${DISCOVER:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  VER="${VERSION:+-$VERSION}"
  SRC="${DISCOVER_SRC:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-fw1}"
  DN="${EVAL_N:-60}"
  export RLGYM_IAC_TRAIN_N="${TRAIN_N:-600}"
  [ "${EVAL_VLLM:-1}" = "1" ] && export RLGYM_EVAL_VLLM=1
  RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/discover$VER-$(date +%Y%m%d-%H%M).txt"
  save_res() { python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true; }
  export RLGYM_IAC_DISCOVERED_RULES=1
  { echo "===== DISCOVER 1/3: fw1 under the DISCOVERED kms_key_policy rule (the blind spot) ====="
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$SRC" --grpo_label "fw1 (pre-discover)" || true
    echo "----- holdout -----"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --split holdout --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$SRC" --grpo_label "fw1 (pre-discover)" || true; } 2>&1 | tee -a "$RES"; save_res
  [ -n "${BOOST_TYPE:-}" ] && { export RLGYM_IAC_BOOST_TYPE="$BOOST_TYPE" RLGYM_IAC_BOOST_N="${BOOST_N:-5}"; echo "### boosting train episodes: $BOOST_TYPE x${BOOST_N:-5} (targeted signal for the sparse rule)"; }
  echo "### DISCOVER continual update: ${DISC_STEPS:-60} steps from fw1, reward now includes kms_key_policy"
  python -m rl_gym.gym.train --env iac --data_dir real --model "$SRC" --lora \
    --num_generations "${NUM_GEN:-4}" --max_steps "${DISC_STEPS:-60}" --lr 1e-5 \
    --beta "${BETA:-0.04}" --vllm_gpu_mem "${VLLM_GPU_MEM:-0.3}" \
    --epsilon_high 0.28 --loss_type dapo \
    --max_completion_len "${MAX_COMPLETION_LEN:-1024}" --max_prompt_len 1536 \
    --output_dir "$OUT/iac-grpo-fw2disc" || { echo "### discover update FAILED"; exit 1; }
  [ -f "$OUT/iac-grpo-fw2disc/COLLAPSE_ALARM" ] && { echo "### !!! COLLAPSE_ALARM:"; cat "$OUT/iac-grpo-fw2disc/COLLAPSE_ALARM"; }
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$OUT/iac-grpo-fw2disc" --prefix "$S3_PREFIX/iac-grpo-fw2disc$VER" || true
  { echo "===== DISCOVER 2/3: updated model under the discovered rule (RECOVERY) ====="
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$OUT/iac-grpo-fw2disc" --grpo_label "fw2 (discover-trained)" || true
    echo "----- holdout -----"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --split holdout --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$OUT/iac-grpo-fw2disc" --grpo_label "fw2 (discover-trained)" || true; } 2>&1 | tee -a "$RES"; save_res
  unset RLGYM_IAC_DISCOVERED_RULES
  { echo "===== DISCOVER 3/3: fw2 under the ORIGINAL 18 rules (no forgetting) ====="
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$OUT/iac-grpo-fw2disc" --grpo_label "fw2 (discover-trained)" || true; } 2>&1 | tee -a "$RES"; save_res
  echo "### discover results -> s3://$S3_BUCKET/$S3_PREFIX/evalres/"
  exit 0
fi

# DISCSFT=1: close the discovered rule via SFT-inject (Option A). SFT fw1 on big-model-written,
# scanner-VERIFIED KMS-correct configs (teach the form the model never learned), then GRPO
# reinforces under the FIXED verifier + KMS boost. Requires the kms-key-policy false-positive fix.
if [ -n "${DISCSFT:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  VER="${VERSION:+-$VERSION}"
  SRC="${DISCOVER_SRC:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-fw1}"
  SFT_DATA="${SFT_DATA:-rl_gym/iac/data/kms_sft.jsonl}"
  DN="${EVAL_N:-60}"
  export RLGYM_IAC_TRAIN_N="${TRAIN_N:-600}"
  export RLGYM_IAC_DISCOVERED_RULES=1
  [ -n "${BOOST_TYPE:-}" ] && export RLGYM_IAC_BOOST_TYPE="$BOOST_TYPE" RLGYM_IAC_BOOST_N="${BOOST_N:-5}"
  [ "${EVAL_VLLM:-1}" = "1" ] && export RLGYM_EVAL_VLLM=1
  RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/discsft$VER-$(date +%Y%m%d-%H%M).txt"
  save_res() { python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true; }
  { echo "===== DISCSFT 1/3: fw1 under the discovered kms_key_policy rule (the blind spot) ====="
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$SRC" --grpo_label "fw1 (pre-sft)" || true
    echo "----- holdout -----"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --split holdout --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$SRC" --grpo_label "fw1 (pre-sft)" || true; } 2>&1 | tee -a "$RES"; save_res
  echo "### SFT-inject the KMS form: $(wc -l < "$SFT_DATA" 2>/dev/null) examples from $SFT_DATA, init=$SRC"
  # KMS configs are long (~1500 tok); fp32 granite-8B @ max_len 1600 OOM'd at micro-batch 2
  # (only 626 MiB over 80 GiB). Halve the micro-batch (2->1), double accum (8->16): same
  # effective batch 16, peak activations halved. Override via SFT_MICROBATCH / SFT_ACCUM.
  RLGYM_SFT_MICROBATCH="${SFT_MICROBATCH:-1}" RLGYM_SFT_ACCUM="${SFT_ACCUM:-16}" \
  python -m rl_gym.gym.sft --env iac --model "$SRC" --data_file "$SFT_DATA" \
    --lora --epochs "${SFT_EPOCHS:-3}" --max_len 1600 --output_dir "$OUT/kms-sft" \
    || { echo "### SFT-inject FAILED"; exit 1; }
  echo "### GRPO from the SFT checkpoint (KMS rule ON + boost, fixed verifier)"
  python -m rl_gym.gym.train --env iac --data_dir real --model "$OUT/kms-sft" --lora \
    --num_generations "${NUM_GEN:-4}" --max_steps "${DISC_STEPS:-100}" --lr 1e-5 \
    --beta "${BETA:-0.04}" --vllm_gpu_mem "${VLLM_GPU_MEM:-0.3}" \
    --epsilon_high 0.28 --loss_type dapo \
    --max_completion_len "${MAX_COMPLETION_LEN:-1024}" --max_prompt_len 1536 \
    --output_dir "$OUT/iac-grpo-fw2sft" || { echo "### GRPO FAILED"; exit 1; }
  [ -f "$OUT/iac-grpo-fw2sft/COLLAPSE_ALARM" ] && { echo "### !!! COLLAPSE_ALARM:"; cat "$OUT/iac-grpo-fw2sft/COLLAPSE_ALARM"; }
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$OUT/iac-grpo-fw2sft" --prefix "$S3_PREFIX/iac-grpo-fw2sft$VER" || true
  { echo "===== DISCSFT 2/3: SFT+GRPO model under the discovered rule (RECOVERY) ====="
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$OUT/iac-grpo-fw2sft" --grpo_label "fw2 (sft+grpo)" || true
    echo "----- holdout -----"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --split holdout --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$OUT/iac-grpo-fw2sft" --grpo_label "fw2 (sft+grpo)" || true; } 2>&1 | tee -a "$RES"; save_res
  unset RLGYM_IAC_DISCOVERED_RULES
  { echo "===== DISCSFT 3/3: fw2 under the ORIGINAL 18 rules (no forgetting) ====="
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$OUT/iac-grpo-fw2sft" --grpo_label "fw2 (sft+grpo)" || true; } 2>&1 | tee -a "$RES"; save_res
  echo "### discsft results -> s3://$S3_BUCKET/$S3_PREFIX/evalres/"
  exit 0
fi

# KMSEVAL=1: settle whether SFT-inject closed the discovered blind spot. The 60-episode
# aggregate can't resolve it (KMS is ~9% of the benchmark); this measures the kms_key_policy
# pass rate on KMS episodes ONLY — real held-out (dev+holdout, OOD) + fresh-seed parametric
# (in-distribution, held-out instances). Guards that fw2 is actually in S3 before the GPU load.
if [ -n "${KMSEVAL:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  FW1="${FW1:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-fw1}"
  FW2="${FW2:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-fw2sft-discsft}"
  export RLGYM_IAC_DISCOVERED_RULES=1
  [ "${EVAL_VLLM:-1}" = "1" ] && export RLGYM_EVAL_VLLM=1
  RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/kmseval-$(date +%Y%m%d-%H%M).txt"
  echo "### guard: confirm fw2 weights present in S3 ($FW2)"
  python -c "from rl_gym.gym.s3io import materialize; import os; p=materialize('$FW2'); fs=os.listdir(p); print('FW2 files:',len(fs), fs[:6]); assert any(('safetensor' in f) or f.endswith('.bin') for f in fs), 'FW2 WEIGHTS MISSING IN S3'" \
    || { echo "### FW2 NOT IN S3 — cannot eval; retrain+eval in one job instead"; exit 3; }
  { echo "===== KMS-SPECIFIC EVAL — kms_key_policy pass rate, fw1 vs fw2 ====="
    python scripts/kms_eval.py --models "fw1=$FW1,fw2=$FW2" \
      --param_n "${PARAM_N:-40}" --param_seed "${PARAM_SEED:-7}" --tok_fallback "$MODEL_ARG"; } 2>&1 | tee -a "$RES"
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true
  echo "### kmseval -> s3://$S3_BUCKET/$S3_PREFIX/evalres/"
  exit 0
fi

# KMSSFT=1: isolate the GRPO wash-out. DISCSFT only measured post-SFT+GRPO; this measures
# the model right after SFT with NO GRPO. kms_sft.jsonl is already ~84% parametric-phrased,
# so no data regen — this directly answers "did SFT alone close it, and did GRPO erase it?".
if [ -n "${KMSSFT:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  SRC="${DISCOVER_SRC:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-fw1}"
  SFT_DATA="${SFT_DATA:-rl_gym/iac/data/kms_sft.jsonl}"
  DN="${EVAL_N:-60}"
  export RLGYM_IAC_DISCOVERED_RULES=1
  [ "${EVAL_VLLM:-1}" = "1" ] && export RLGYM_EVAL_VLLM=1
  RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/kmssft-$(date +%Y%m%d-%H%M).txt"
  echo "### SFT-only (NO GRPO): $(wc -l < "$SFT_DATA" 2>/dev/null) examples, init=$SRC"
  RLGYM_SFT_MICROBATCH="${SFT_MICROBATCH:-1}" RLGYM_SFT_ACCUM="${SFT_ACCUM:-16}" \
  python -m rl_gym.gym.sft --env iac --model "$SRC" --data_file "$SFT_DATA" \
    --lora --epochs "${SFT_EPOCHS:-3}" --max_len 1600 --output_dir "$OUT/kms-sft-only" \
    || { echo "### SFT FAILED"; exit 1; }
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$OUT/kms-sft-only" --prefix "$S3_PREFIX/iac-sft-kmsonly" || true
  { echo "===== KMSSFT: KMS-specific pass rate — fw1 vs SFT-only (NO GRPO) ====="
    python scripts/kms_eval.py --models "fw1=$SRC,sft-only=$OUT/kms-sft-only" \
      --param_n "${PARAM_N:-40}" --param_seed "${PARAM_SEED:-7}" --tok_fallback "$MODEL_ARG"; } 2>&1 | tee -a "$RES"
  unset RLGYM_IAC_DISCOVERED_RULES
  { echo "===== KMSSFT: no forgetting — SFT-only under the ORIGINAL 18 rules ====="
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$OUT/kms-sft-only" --grpo_label "sft-only" || true
    echo "----- holdout -----"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --split holdout --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$OUT/kms-sft-only" --grpo_label "sft-only" || true; } 2>&1 | tee -a "$RES"
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true
  echo "### kmssft -> s3://$S3_BUCKET/$S3_PREFIX/evalres/"
  exit 0
fi

# CATRES=<filename-substring>: recover a result file from S3 evalres/ that the job-log cap
# ate (huge vLLM config dumps push short eval lines out of the 203-line log window). Downloads
# evalres, prints ONLY the short result lines — which survive because nothing noisy follows.
if [ -n "${CATRES:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  python - <<'PY'
import os, glob
from rl_gym.gym.s3io import materialize
pat = os.environ["CATRES"]
p = materialize(f"s3://{os.environ['S3_BUCKET']}/{os.environ.get('S3_PREFIX','rl-gym-iac')}/evalres")
keys = ("kms_policy_pass", "heldout", "KMS-SPECIFIC", "param_seed", "=====",
        "random", "base LLM", "sft-only", "fw1", "oracle", "no forgetting")
for f in sorted(glob.glob(p + "/*")):
    if pat in os.path.basename(f):
        print(f"\n=== RESFILE {os.path.basename(f)} ===", flush=True)
        for line in open(f, errors="ignore"):
            if any(k in line for k in keys):
                print(line.rstrip(), flush=True)
PY
  echo "### catres done"
  exit 0
fi

# STAR=1: STaR / rejection-sampling self-distillation to close the KMS blind spot. Each round:
# sample the current model, keep scanner-VERIFIED policy-passing completions, SFT on them (r64,
# NO GRPO), re-measure. Raises the rare key-policy sampling rate from the model's own successes;
# compounds across rounds. Starts from sft-only (7.5%) — a better launch pad than fw1's 2.5%.
if [ -n "${STAR:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  M="${DISCOVER_SRC:-s3://$S3_BUCKET/$S3_PREFIX/iac-sft-kmsonly}"
  FW1="s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-fw1"
  DN="${EVAL_N:-60}"; ROUNDS="${STAR_ROUNDS:-3}"; RANK="${STAR_RANK:-64}"
  export RLGYM_IAC_DISCOVERED_RULES=1
  [ "${EVAL_VLLM:-1}" = "1" ] && export RLGYM_EVAL_VLLM=1
  RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/star-$(date +%Y%m%d-%H%M).txt"
  save_res() { python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true; }
  echo "### guard: confirm STaR init weights present ($M)"
  python -c "from rl_gym.gym.s3io import materialize; import os; p=materialize('$M'); fs=os.listdir(p); print('init files:',len(fs)); assert any(('safetensor' in f) or f.endswith('.bin') for f in fs), 'INIT MISSING'" \
    || { echo "### STaR init NOT in S3 — falling back to fw1"; M="$FW1"; }
  for r in $(seq 1 "$ROUNDS"); do
    echo "===== STAR round $r / $ROUNDS — sample from: $M =====" | tee -a "$RES"
    python scripts/star_sample.py --model "$M" --out "$OUT/star_r$r.jsonl" \
      --n_episodes "${STAR_EPS:-150}" --samples "${STAR_N:-20}" --temp "${STAR_TEMP:-1.0}" \
      --gen_seed "$r" --tok_fallback "$MODEL_ARG" 2>&1 | tee -a "$RES" || { echo "### sample failed r$r"; break; }
    NK=$(wc -l < "$OUT/star_r$r.jsonl" 2>/dev/null || echo 0)
    if [ "${NK:-0}" -lt 10 ]; then echo "### round $r harvested only $NK — STaR stalled, stopping" | tee -a "$RES"; break; fi
    echo "### round $r: SFT on $NK harvested targets (LoRA r=$RANK, epochs ${STAR_EPOCHS:-3})" | tee -a "$RES"
    RLGYM_SFT_MICROBATCH="${SFT_MICROBATCH:-1}" RLGYM_SFT_ACCUM="${SFT_ACCUM:-16}" \
    python -m rl_gym.gym.sft --env iac --model "$M" --data_file "$OUT/star_r$r.jsonl" \
      --lora --lora_r "$RANK" --lora_alpha "$((RANK*2))" --epochs "${STAR_EPOCHS:-3}" \
      --max_len 1600 --output_dir "$OUT/star-m$r" || { echo "### sft failed r$r"; break; }
    M="$OUT/star-m$r"
    { echo "----- round $r eval (kms_key_policy pass rate) -----"
      python scripts/kms_eval.py --models "round$r=$M" --param_n 40 --param_seed 7 \
        --tok_fallback "$MODEL_ARG"; } 2>&1 | tee -a "$RES"; save_res
  done
  echo "### STaR done. Final model: $M"
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$M" --prefix "$S3_PREFIX/iac-star-final" || true
  { echo "===== STAR FINAL: fw1 vs star-final (kms pass rate) ====="
    python scripts/kms_eval.py --models "fw1=$FW1,star-final=$M" --param_n 40 --param_seed 7 \
      --tok_fallback "$MODEL_ARG"; } 2>&1 | tee -a "$RES"; save_res
  unset RLGYM_IAC_DISCOVERED_RULES
  { echo "===== STAR FINAL: no forgetting — star-final under the ORIGINAL 18 rules ====="
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$M" --grpo_label "star-final" || true
    echo "----- holdout -----"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --split holdout --n_episodes "$DN" \
      --base "$MODEL_ARG" --grpo "$M" --grpo_label "star-final" || true; } 2>&1 | tee -a "$RES"; save_res
  echo "### star -> s3://$S3_BUCKET/$S3_PREFIX/evalres/"
  exit 0
fi

# REPAIR=1: v18 — train the repair turn (generate->scan->repair as single-turn GRPO).
# INIT = the checkpoint to improve (default: the promoted clean-split model). Prep
# rolls turn 1 with INIT, imperfect configs + real findings become repair episodes;
# GRPO trains on the repair/gen mix with the unchanged scanner reward. Tables:
# single-pass (ratchet: no regression vs INIT) + two-pass system (the headline).
if [ -n "${REPAIR:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  VER="${VERSION:+-$VERSION}"
  INIT="${INIT:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-v16r}"
  export RLGYM_IAC_TRAIN_N="${TRAIN_N:-1200}"
  [ "${EVAL_VLLM:-1}" = "1" ] && export RLGYM_EVAL_VLLM=1
  RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/results$VER-$(date +%Y%m%d-%H%M).txt"
  save_res() { [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true; }

  echo "### REPAIR prep: turn-1 rollout + findings with INIT=$INIT"
  python -m rl_gym.gym.repair_prep --env iac --data_dir real --model "$INIT" \
    --max_completion_len "${MAX_COMPLETION_LEN:-1024}" --out_dir "$OUT/repair"
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$OUT/repair" --prefix "$S3_PREFIX/repair$VER" || true

  echo "### GRPO on the repair mix, init=$INIT"
  python -m rl_gym.gym.train --env iac_repair --data_dir "$OUT/repair" --model "$INIT" --lora \
    --num_generations "${NUM_GEN:-4}" --max_steps "${GRPO_STEPS:-120}" --lr 1e-5 \
    --beta "${BETA:-0.04}" --vllm_gpu_mem "${VLLM_GPU_MEM:-0.3}" \
    --epsilon_high 0.28 --loss_type dapo \
    --max_completion_len "${MAX_COMPLETION_LEN:-1024}" --max_prompt_len 1536 \
    --output_dir "$OUT/iac-grpo-repair"
  [ -f "$OUT/iac-grpo-repair/COLLAPSE_ALARM" ] && { echo "### !!! COLLAPSE_ALARM:"; cat "$OUT/iac-grpo-repair/COLLAPSE_ALARM"; }
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$OUT/iac-grpo-repair" --prefix "$S3_PREFIX/iac-grpo$VER" || true

  { echo "================ SINGLE-PASS (ratchet: v18 must not regress) ================"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "${EVAL_N:-120}" \
      --base "$MODEL_ARG" --sft "$INIT" --sft_label "GRPO v16r (init)" \
      --grpo "$OUT/iac-grpo-repair" --grpo_label "v18 (repair-trained)" || true; } 2>&1 | tee -a "$RES"; save_res
  { echo "================ SINGLE-PASS HOLDOUT ================"
    python -m rl_gym.gym.eval compare --env iac --data_dir real --split holdout \
      --n_episodes "${EVAL_N:-120}" --base "$MODEL_ARG" --sft "$INIT" --sft_label "GRPO v16r (init)" \
      --grpo "$OUT/iac-grpo-repair" --grpo_label "v18 (repair-trained)" || true; } 2>&1 | tee -a "$RES"; save_res
  { echo "================ TWO-PASS SYSTEM (generate -> scan -> repair) ================"
    python -m rl_gym.gym.eval repair_compare --env iac --data_dir real --n_episodes "${EVAL_N:-120}" \
      --models "GRPO v16r (untrained rep)=$INIT,v18 (repair-trained)=$OUT/iac-grpo-repair" || true; } 2>&1 | tee -a "$RES"; save_res
  { echo "================ TWO-PASS HOLDOUT ================"
    python -m rl_gym.gym.eval repair_compare --env iac --data_dir real --split holdout \
      --n_episodes "${EVAL_N:-120}" \
      --models "GRPO v16r (untrained rep)=$INIT,v18 (repair-trained)=$OUT/iac-grpo-repair" || true; } 2>&1 | tee -a "$RES"; save_res
  echo "### results persisted to s3://$S3_BUCKET/$S3_PREFIX/evalres/"
  exit 0
fi

# SERVE2X2=1: authoritative serving 2x2 (pass@1/best-of-N x +-repair) on the anchors.
if [ -n "${SERVE2X2:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/serve2x2-$(date +%Y%m%d-%H%M).txt"
  python scripts/serve_2x2.py --model "${CANDIDATE:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-fw1}" \
    --n "${GATE_N:-60}" --n_bo "${N_BO:-4}" 2>&1 | tee -a "$RES"
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true
  echo "### serve2x2 persisted to s3://$S3_BUCKET/$S3_PREFIX/evalres/"
  exit 0
fi

# FWSTATS=1: flywheel gain at pass@1 vs best-of-N — eval incumbent + candidate at both
# sampling modes on the anchors (decides whether best-of-N still earns its serving cost).
if [ -n "${FWSTATS:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/fwstats-$(date +%Y%m%d-%H%M).txt"
  python scripts/flywheel_stats.py \
    --incumbent "${INCUMBENT:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-v18}" \
    --candidate "${CANDIDATE:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-fw1}" \
    --n "${GATE_N:-60}" --n_bo "${N_BO:-4}" 2>&1 | tee -a "$RES"
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true
  echo "### fwstats persisted to s3://$S3_BUCKET/$S3_PREFIX/evalres/"
  exit 0
fi

# FLYWHEEL=1: the standing loop — aggregate SERVED episodes from S3 (real traffic:
# requests + repair transcripts logged by the webdemo), run one continual update from
# the incumbent, then the executable promotion gate (gym.gate) decides: PROMOTE
# (versioned upload + marker) or BLOCK (incumbent stays). Serving traffic literally
# becomes next week's model — or gets refused by the ratchet. Either way, receipts.
if [ -n "${FLYWHEEL:-}" ]; then
  S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
  VER="${VERSION:+-$VERSION}"
  INIT="${INIT:-s3://$S3_BUCKET/$S3_PREFIX/iac-grpo-v18}"
  export RLGYM_IAC_TRAIN_N="${TRAIN_N:-1200}"
  [ "${EVAL_VLLM:-1}" = "1" ] && export RLGYM_EVAL_VLLM=1
  RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/results$VER-$(date +%Y%m%d-%H%M).txt"
  save_res() { [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true; }

  echo "### FLYWHEEL 1/4: aggregate served traffic from s3://$S3_BUCKET/$S3_PREFIX/served/"
  if ! python scripts/flywheel_aggregate.py --out_dir "$OUT/flywheel" \
        --min_repair "${FW_MIN_REPAIR:-6}" --replay "${FW_REPLAY:-200}" 2>&1 | tee -a "$RES"; then
    echo "### FLYWHEEL: not enough served traffic — no cycle warranted"; save_res; exit 0
  fi
  save_res
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$OUT/flywheel" --prefix "$S3_PREFIX/flywheel$VER" || true

  echo "### FLYWHEEL 2/4: continual update from $INIT on served+replay mix"
  python -m rl_gym.gym.train --env iac_repair --data_dir "$OUT/flywheel" --model "$INIT" --lora \
    --num_generations "${NUM_GEN:-4}" --max_steps "${FW_STEPS:-50}" --lr 1e-5 \
    --beta "${BETA:-0.04}" --vllm_gpu_mem "${VLLM_GPU_MEM:-0.3}" \
    --epsilon_high 0.28 --loss_type dapo \
    --max_completion_len "${MAX_COMPLETION_LEN:-1024}" --max_prompt_len 1536 \
    --output_dir "$OUT/iac-grpo-fw"

  echo "### FLYWHEEL 3/4: promotion gate (frozen anchors + collapse alarm)"
  if python -m rl_gym.gym.gate --candidate "$OUT/iac-grpo-fw" --incumbent "$INIT" \
       --n "${GATE_N:-60}" --tol "${GATE_TOL:-0.01}" > "$RES_DIR/gate$VER.txt" 2>&1; then
    GATE=0; else GATE=$?; fi
  cat "$RES_DIR/gate$VER.txt" | tee -a "$RES"; save_res

  if [ "$GATE" = "0" ]; then
    echo "### FLYWHEEL 4/4: PROMOTED -> s3://$S3_BUCKET/$S3_PREFIX/iac-grpo$VER"
    python -m rl_gym.gym.s3io upload --local "$OUT/iac-grpo-fw" --prefix "$S3_PREFIX/iac-grpo$VER"
    echo "PROMOTED $(date -u +%FT%TZ) iac-grpo$VER (from $INIT, gate tol ${GATE_TOL:-0.01})" > "$RES_DIR/PROMOTED$VER.txt"
  else
    echo "### FLYWHEEL 4/4: BLOCKED by gate (exit $GATE) — incumbent $INIT stays"
    echo "BLOCKED $(date -u +%FT%TZ) candidate iac-grpo-fw (gate exit $GATE)" > "$RES_DIR/BLOCKED$VER.txt"
  fi
  save_res
  exit 0
fi

# ---- v17 staging: versioned artifacts + results that survive log expiry ----
S3_PREFIX="${S3_PREFIX:-rl-gym-iac}"
VER="${VERSION:+-$VERSION}"          # VERSION=v17 -> "-v17" suffix on every S3 artifact
export RLGYM_IAC_TRAIN_N="${TRAIN_N:-600}"
RES_DIR="$OUT/evalres"; mkdir -p "$RES_DIR"; RES="$RES_DIR/results${VER}-$(date +%Y%m%d-%H%M).txt"
save_res() { [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$RES_DIR" --prefix "$S3_PREFIX/evalres" || true; }

# RAFT=1: rejection-sampling stage (Dong et al. RAFT) — the base model's own
# verifier-perfect answers on REAL prompts become the SFT targets (no more imitating
# templated oracles), and its per-episode stats become the GRPO difficulty-band filter
# (DAPO-style curriculum, computed once). Non-fatal on failure.
SFT_EXTRA=""; GRPO_EXTRA=""
if [ -n "${RAFT:-}" ]; then
  echo "### RAFT: best-of-${RAFT_N:-8} with the base on real train prompts"
  python -m rl_gym.gym.raft --env iac --data_dir real --model "$MODEL_ARG" \
    --n "${RAFT_N:-8}" --max_completion_len "${MAX_COMPLETION_LEN:-1024}" \
    --out_dir "$OUT/raft" \
    || echo "### WARN: RAFT failed — SFT falls back to parametric oracles"
  [ -s "$OUT/raft/sft.jsonl" ]   && SFT_EXTRA="--data_file $OUT/raft/sft.jsonl --mix_oracle 120"
  [ -s "$OUT/raft/stats.jsonl" ] && GRPO_EXTRA="--stats_file $OUT/raft/stats.jsonl --band ${BAND:-0.05,0.95}"
  [ -n "${S3_BUCKET:-}" ] && python -m rl_gym.gym.s3io upload --local "$OUT/raft" --prefix "$S3_PREFIX/raft$VER" || true
fi

# SFT warm-start: imitate the terse, fully-hardened oracle (or, with RAFT, the model's
# own scanner-perfect real-prompt answers). v8 showed GRPO-from-base regressed because
# the base pads verbose prose that overruns the length cap (~80% clipped). Skip with SKIP_SFT=1.
GRPO_INIT="$MODEL_ARG"
if [ -z "${SKIP_SFT:-}" ]; then
  echo "### SFT warm-start on --env iac (${SFT_EXTRA:-parametric oracles})"
  # Non-fatal: if SFT diverges (the NaN guard in gym/sft.py exits without saving),
  # continue with GRPO from the base model rather than killing the whole job.
  python -m rl_gym.gym.sft --env iac --model "$MODEL_ARG" --lora \
    --epochs "${SFT_EPOCHS:-2}" --lr 1e-5 --max_len 1280 --output_dir "$OUT/iac-sft" \
    $SFT_EXTRA \
    || echo "### WARN: SFT failed/diverged — GRPO will start from base"
  # real weights present? (Trainer creates the dir even when the run fails)
  if ls "$OUT/iac-sft"/*.safetensors >/dev/null 2>&1; then
    GRPO_INIT="$OUT/iac-sft"
  fi
fi

# GRPO from the SFT checkpoint, training on the REAL mix (--data_dir real: parametric +
# IaC-Eval + GitHub-mined requests; the scanner reward needs no reference). SFT above
# stays parametric on purpose — it imitates *hardened* oracles only. Length fixes vs v8:
# cap 640->1024 and drop --mask_truncated (v8 masked ~80% of rollouts). Keep Clip-Higher + dapo.
echo "### GRPO (RLVR) on --env iac — 8B + LoRA, init=$GRPO_INIT, real-mix data, vLLM colocate"
python -m rl_gym.gym.train --env iac --data_dir real --model "$GRPO_INIT" --lora \
  --num_generations "$NUM_GEN" --max_steps "$GRPO_STEPS" --lr 1e-5 \
  --beta "${BETA:-0.04}" --vllm_gpu_mem "${VLLM_GPU_MEM:-0.3}" \
  --epsilon_high 0.28 --loss_type dapo ${DYNSAMP:+--dynamic_sampling} \
  $GRPO_EXTRA \
  --max_completion_len "${MAX_COMPLETION_LEN:-1024}" --max_prompt_len 512 \
  --output_dir "$OUT/iac-grpo"
[ -f "$OUT/iac-grpo/COLLAPSE_ALARM" ] && { echo "### !!! COLLAPSE_ALARM raised during GRPO:"; cat "$OUT/iac-grpo/COLLAPSE_ALARM"; }

# Persist the trained models FIRST — a flaky eval shouldn't lose them (ephemeral disk).
if [ -n "${S3_BUCKET:-}" ] && [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then
  echo "### saving models to s3://$S3_BUCKET/$S3_PREFIX/ (suffix '$VER')"
  [ -d "$OUT/iac-sft" ]  && python -m rl_gym.gym.s3io upload --local "$OUT/iac-sft"  --prefix "$S3_PREFIX/iac-sft$VER"
  [ -d "$OUT/iac-grpo" ] && python -m rl_gym.gym.s3io upload --local "$OUT/iac-grpo" --prefix "$S3_PREFIX/iac-grpo$VER"
else
  echo "### S3 save skipped (set S3_BUCKET + AWS creds to persist)"
fi

# Compare base -> SFT -> GRPO -> oracle so we see the warm-start's lift AND GRPO's lift
# over it. Tolerate eval failure (|| true) so the saved models + earlier output survive.
# EVERY table also lands in S3 (tee + save_res) — log retention burned us once (eval120).
SFT_ARG=""; [ -d "$OUT/iac-sft" ] && SFT_ARG="--sft $OUT/iac-sft"
EVAL_N="${EVAL_N:-40}"; AUDIT_N="${AUDIT_N:-$EVAL_N}"
[ "${EVAL_VLLM:-1}" = "1" ] && export RLGYM_EVAL_VLLM=1   # batched fast eval on CUDA
{ echo "================ RESULTS (base -> SFT -> GRPO -> oracle) ================"
  python -m rl_gym.gym.eval compare --env iac --n_episodes "$EVAL_N" --base "$MODEL_ARG" $SFT_ARG --grpo "$OUT/iac-grpo" || true; } 2>&1 | tee -a "$RES"; save_res

{ echo "================ REWARD-HACKING AUDIT ================"
  python -m rl_gym.gym.eval audit --env iac --n_episodes "$AUDIT_N" --base "$MODEL_ARG" $SFT_ARG --grpo "$OUT/iac-grpo" || true; } 2>&1 | tee -a "$RES"; save_res

# Real-world eval: IaC-Eval (human-curated NL->Terraform, CC-BY-4.0). Here the "oracle"
# row is the human reference — NOT a ceiling (refs score ~0.45 security, ~19% critical),
# so a security-trained policy beating it is the headline result.
{ echo "================ REAL-WORLD EVAL (IaC-Eval) ================"
  python -m rl_gym.gym.eval compare --env iac --data_dir real --n_episodes "$EVAL_N" --base "$MODEL_ARG" $SFT_ARG --grpo "$OUT/iac-grpo" || true; } 2>&1 | tee -a "$RES"; save_res

# Holdout eval: mined episodes (Checkov corpus + GitHub) hash-disjoint from the train
# mix — the second frozen benchmark that stays virgin even with IaC-Eval train in-mix.
{ echo "================ HOLDOUT EVAL (mined, never-trained source) ================"
  python -m rl_gym.gym.eval compare --env iac --data_dir real --split holdout \
    --n_episodes "$EVAL_N" --base "$MODEL_ARG" $SFT_ARG --grpo "$OUT/iac-grpo" || true; } 2>&1 | tee -a "$RES"; save_res

# DRIFT=1: continual-learning demo — "the security standard changed". Enable the 5
# held-out rules (never rewarded during v17), measure the drop, run one SHORT update
# from the v17 checkpoint, then prove recovery under the new scanner AND no
# forgetting under the old one. Shared with EVAL_ONLY via run_drift().
if [ -n "${DRIFT:-}" ] && ls "$OUT/iac-grpo"/*.safetensors >/dev/null 2>&1; then
  run_drift "$OUT/iac-grpo"
fi
echo "### results persisted to s3://${S3_BUCKET:-<unset>}/$S3_PREFIX/evalres/"
