#!/usr/bin/env bash
# Submit the IaC GRPO kill-test as a Nebius AI Job — reconstructed from `nebius ai job
# create` (the original ad-hoc command wasn't saved). Run in a terminal where the
# `nebius` CLI is authenticated for the rl-gym profile.
#
# The job is bounded + self-terminating. It:
#   1. pulls the prebuilt image (torch+vLLM+trl baked) from PRIVATE GHCR,
#   2. clones this repo over HTTPS with a GitHub token, and
#   3. runs scripts/nebius_iac.sh (GRPO on --env iac -> [save to S3] -> compare + audit).
#
# ── Two credential modes ─────────────────────────────────────────────────────────────
#   INLINE (set GH_PAT):  pass the GitHub PAT straight to the job — NO MysteryBox writes.
#                         Simplest; the PAT lands in the job spec (fine for a personal run).
#   SECRET (default):     use MysteryBox secrets rl-gym-ghcr / rl-gym-gh (run nebius_secrets.sh
#                         first). Use this once MysteryBox write-auth works.
#   The PAT is a GitHub *classic* token with scopes: repo + read:packages.
#
# ── S3 persistence (optional) ────────────────────────────────────────────────────────
#   Set S3_BUCKET to persist the model + use the base-weights cache (needs the readable
#   secret rl-gym-s3-creds). Leave it unset for a logs-only validation run (Qwen is public,
#   so no HF token needed; scripts/nebius_iac.sh just skips the S3 steps).
#
# ── Quickest first run ───────────────────────────────────────────────────────────────
#   PROJECT_ID=project-xxxx GH_PAT=ghp_xxxx bash scripts/nebius_launch_iac.sh
set -euo pipefail
# Confirmed rl-gym project + bucket (nik profile, verified 2026-07-03). Override via env.
PROJECT_ID="${PROJECT_ID:-project-e00v3daxpr000ekf7jkasw}"
S3_BUCKET="${S3_BUCKET:-green-meadowlark-bucket-7}"   # set S3_BUCKET= (empty) for a logs-only run

PROFILE="${PROFILE:-nik}"
REPO="${REPO:-SkyForce/rl-gym}"
GH_USER="${GH_USER:-SkyForce}"
IMAGE="${IMAGE:-ghcr.io/skyforce/rl-gym-gpu:latest}"

# Training needs the H100; SERVING a 7B demo doesn't — use PLATFORM=gpu-l40s-d for
# webdemo (48GB fits two bf16 7B engines; ~1/3 the H100 rate, ~2-3x slower generation).
# PRESET="" lets the platform pick its minimum preset (H100 preset names don't exist on L40S).
PLATFORM="${PLATFORM:-gpu-h100-sxm}"
PRESET="${PRESET-1gpu-16vcpu-200gb}"
DISK="${DISK:-512Gi}"
TIMEOUT="${TIMEOUT:-3h}"   # v12: full pipeline + 3 eval tables overran 2h

# MysteryBox secret names (SECRET mode only).
REGISTRY_SECRET="${REGISTRY_SECRET:-rl-gym-ghcr}"
GH_TOKEN_SECRET="${GH_TOKEN_SECRET:-rl-gym-gh}"
S3_CREDS_SECRET="${S3_CREDS_SECRET:-rl-gym-s3-creds}"

ARGS=(
  --profile "$PROFILE" --parent-id "$PROJECT_ID"
  --name "iac-grpo-$(date +%Y%m%d-%H%M%S)"
  --platform "$PLATFORM"
  --disk-size "$DISK" --timeout "$TIMEOUT"
  --image "$IMAGE"
)
[ -n "$PRESET" ] && ARGS+=( --preset "$PRESET" )

# --- repo clone + registry pull creds ---
# DEFAULT = PUBLIC: the repo and the prebuilt GHCR image are public, so reproduction needs NO
# GitHub token. Set GH_PAT to use a private fork/image (INLINE), or USE_SECRET=1 for MysteryBox.
if [ -n "${GH_PAT:-}" ]; then
  echo "### credential mode: INLINE (private fork/image via GH_PAT)"
  ARGS+=( --registry-username "$GH_USER" --registry-password "$GH_PAT" --env "GH_TOKEN=$GH_PAT" )
  CLONE_URL='https://x-access-token:${GH_TOKEN}@github.com/'"$REPO"'.git'
elif [ -n "${USE_SECRET:-}" ]; then
  echo "### credential mode: SECRET (MysteryBox: $REGISTRY_SECRET / $GH_TOKEN_SECRET)"
  ARGS+=( --registry-secret "$REGISTRY_SECRET" --env-secret "GH_TOKEN=$GH_TOKEN_SECRET" )
  CLONE_URL='https://x-access-token:${GH_TOKEN}@github.com/'"$REPO"'.git'
else
  echo "### credential mode: PUBLIC (public repo + public GHCR image — no token needed)"
  CLONE_URL='https://github.com/'"$REPO"'.git'
fi
# ${GH_TOKEN}, when present, stays literal here; it is expanded INSIDE the container, never locally.
JOB_SCRIPT='set -e; git clone --depth 1 '"$CLONE_URL"' /workspace/rl-gym; cd /workspace/rl-gym; bash scripts/nebius_iac.sh'

# --- S3 persistence (optional) ---
if [ -n "${S3_BUCKET:-}" ]; then
  echo "### S3 persistence ON (bucket=$S3_BUCKET via $S3_CREDS_SECRET)"
  ARGS+=( --env-secret "AWS_ACCESS_KEY_ID=$S3_CREDS_SECRET"
          --env-secret "AWS_SECRET_ACCESS_KEY=$S3_CREDS_SECRET"
          --env "S3_BUCKET=$S3_BUCKET" --env "S3_PREFIX=rl-gym-iac" )
else
  echo "### S3 persistence OFF — logs-only validation run"
fi

# EVAL_ONLY=1: no training — just the 3 tables against the models already in S3
if [ -n "${EVAL_ONLY:-}" ]; then
  ARGS+=( --env "EVAL_ONLY=1" )
  [ -n "${N_EPISODES:-}" ] && ARGS+=( --env "N_EPISODES=$N_EPISODES" )
fi
# BAKEOFF=1: base-model bake-off on the real dev split (no training)
if [ -n "${BAKEOFF:-}" ]; then
  ARGS+=( --env "BAKEOFF=1" )
  [ -n "${N_EPISODES:-}" ] && ARGS+=( --env "N_EPISODES=$N_EPISODES" )
fi
# DEMO=1: showcase scorecards (tuned vs base) from the S3 model — short job
if [ -n "${DEMO:-}" ]; then
  ARGS+=( --env "DEMO=1" )
  [ -n "${DEMO_EPISODES:-}" ] && ARGS+=( --env "DEMO_EPISODES=$DEMO_EPISODES" )
fi
# WEBDEMO=1: live browser demo — job stays up until --timeout (billing boundary)
if [ -n "${WEBDEMO:-}" ]; then
  ARGS+=( --env "WEBDEMO=1" --container-port 8000 )
  # frontier API comparison column (quality + $/request); omit the key to disable
  [ -n "${ANTHROPIC_API_KEY:-}" ] && ARGS+=( --env "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" )
  # grow-the-verifier panel: live rule authoring by the big model on Token Factory.
  # Omit the key and the panel stays hidden (generation-only demo). TF_BIG_MODEL overrides.
  [ -n "${TOKEN_FACTORY_API_KEY:-}" ] && ARGS+=( --env "TOKEN_FACTORY_API_KEY=$TOKEN_FACTORY_API_KEY" )
  [ -n "${TF_BIG_MODEL:-}" ]          && ARGS+=( --env "TF_BIG_MODEL=$TF_BIG_MODEL" )
fi
# SSH rescue hatch: without a key at create time, a live job cannot be entered or
# fixed in place (learned when the tunnel died in an otherwise-healthy demo job)
[ -f "$HOME/.ssh/id_ed25519.pub" ] && ARGS+=( --ssh-key "$(cat "$HOME/.ssh/id_ed25519.pub")" )

# Model + generation knobs (Qwen3: pass PROMPT_SUFFIX=" /no_think" to disable hybrid
# thinking uniformly, or leave thinking on and raise MAX_COMPLETION_LEN to ~2560)
MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct}"
ARGS+=( --env "MODEL=$MODEL" )
[ -n "${PROMPT_SUFFIX:-}" ]      && ARGS+=( --env "RLGYM_PROMPT_SUFFIX=$PROMPT_SUFFIX" )
[ -n "${MAX_COMPLETION_LEN:-}" ] && ARGS+=( --env "MAX_COMPLETION_LEN=$MAX_COMPLETION_LEN" )
# v17 pipeline knobs — any of these set locally is forwarded into the job env verbatim
for V in VERSION RAFT RAFT_N BAND DRIFT DRIFT_STEPS DRIFT_EVAL_N TRAIN_N BETA DYNSAMP \
         GRPO_STEPS NUM_GEN EVAL_N AUDIT_N SFT_EPOCHS VLLM_GPU_MEM SKIP_SFT EVAL_VLLM \
         REPAIR INIT WEBDEMO_DRIFT WEBDEMO_GPU_FRAC \
         FLYWHEEL FW_STEPS FW_MIN_REPAIR FW_REPLAY GATE_N GATE_TOL \
         FWSTATS INCUMBENT CANDIDATE N_BO SERVE2X2 CLEAN CONFIRM KEEP \
         MINE MINE_MODEL MINE_OUT DISCOVER DISCOVER_SRC DISC_STEPS BOOST_TYPE BOOST_N \
         RLGYM_IAC_DISCOVERED_RULES RLGYM_IAC_BOOST_TYPE RLGYM_IAC_BOOST_N \
         DISCSFT SFT_DATA DEL KMSEVAL FW1 FW2 PARAM_N PARAM_SEED \
         KMSSFT SFT_MICROBATCH SFT_ACCUM CATRES \
         STAR STAR_ROUNDS STAR_RANK STAR_EPS STAR_N STAR_TEMP STAR_EPOCHS; do
  [ -n "${!V:-}" ] && ARGS+=( --env "$V=${!V}" )
done

ARGS+=( --container-command "bash -lc \"$JOB_SCRIPT\"" )

set -x
nebius ai job create "${ARGS[@]}"
